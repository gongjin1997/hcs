"""通知：路由、联系组、联系人的查看与维护（增删改、路由排序）、路由模拟、SMTP 测试邮件、通知记录、邮件预览。

- 查看：notify:view；维护：notify:manage（通知配置为全局配置，修改需要全部租户权限）
- 路由按优先级（数字小的先匹配）逐条匹配；命中后 continue_match = 否 时停止，= 是 时继续匹配后面的路由（抄送）
"""
import re
import time

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_auditor
from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.response import BizError, ErrorCode, not_found, ok
from app.models import AlarmEvent, Contact, ContactGroup, Notification, NotifyRoute
from app.models.base import utcnow
from app.notify.escalation import escalation_plan
from app.notify.render import render_alarm_mail
from app.notify.routing import resolve_recipients, route_matches
from app.notify.smtp import build_message, send
from app.schemas.alarm import NotificationOut
from app.services import silence_service
from app.services.alarm_service import get_alarm
from app.services.catalog import PLATFORM_TYPES, RESOURCE_TYPES, SERVICES, SEVERITY_LABEL
from app.services.tenancy import tenant_map

router = APIRouter(prefix="/notify", tags=["通知"], dependencies=[Depends(require_perm("notify:view"))])
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SEVERITIES = list(SEVERITY_LABEL)
MATCH_KEYS = ("tenant_ids", "services", "severities", "resource_types")


def manager(scope: Scope = Depends(get_scope), cu: CurrentUser = Depends(require_perm("notify:manage"))) -> CurrentUser:
    if not scope.full:
        raise BizError(ErrorCode.FORBIDDEN, "通知配置为全局配置，修改需要全部租户的数据权限", 403)
    return cu


# ---------------- 入参 ----------------
class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    email: str = Field(max_length=128)
    phone: str | None = Field(None, max_length=32)
    enabled: bool = True

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip()
        if not EMAIL.match(v):
            raise ValueError("邮箱格式不正确")
        return v


class GroupIn(BaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$", description="联系组代码（小写字母开头，创建后不能修改）")
    name: str = Field(min_length=1, max_length=64)
    member_ids: list[int] = []
    leader_ids: list[int] = []


class EscalationIn(BaseModel):
    after_min: int = Field(ge=5, le=1440)
    to: str = "leader"                       # leader = 本路由联系组的组长；其他 = 联系组代码（通知该组全部成员）
    severities: list[str] = []


class RouteIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    priority: int = Field(100, ge=0, le=10000)
    match: dict = {}
    group_code: str
    escalation: list[EscalationIn] = []
    continue_match: bool = False
    enabled: bool = True


class ReorderIn(BaseModel):
    ids: list[int]


class SimulateIn(BaseModel):
    tenant_id: int | None = None
    service: str
    resource_type: str | None = None
    severity: str = "P2"
    rule_id: str = "R-SIM-001"
    resource_name: str = "模拟资源"


class TestMailIn(BaseModel):
    to: list[str] = Field(min_length=1, max_length=10)


# ---------------- 工具 ----------------
def _contacts(db: Session) -> dict[int, Contact]:
    return {c.id: c for c in db.scalars(select(Contact))}


def _pick(contacts: dict[int, Contact], ids: list[int]) -> list[dict]:
    return [{"id": i, "name": contacts[i].name, "email": contacts[i].email, "enabled": contacts[i].enabled}
            for i in ids if i in contacts]


def _check_match(db: Session, m: dict) -> dict:
    out: dict = {}
    for k in MATCH_KEYS:
        v = m.get(k) or []
        if not isinstance(v, list):
            raise BizError(ErrorCode.BAD_REQUEST, f"匹配条件 {k} 应为数组")
        if not v:
            continue
        if k == "tenant_ids":
            known = set(tenant_map(db))
            v = sorted({int(x) for x in v})
            if any(t not in known for t in v):
                raise BizError(ErrorCode.BAD_REQUEST, "匹配条件中包含不存在的租户")
        elif k == "services":
            bad = [x for x in v if x not in SERVICES]
            if bad:
                raise BizError(ErrorCode.BAD_REQUEST, f"未知服务：{'、'.join(bad)}")
        elif k == "severities":
            bad = [x for x in v if x not in SEVERITIES]
            if bad:
                raise BizError(ErrorCode.BAD_REQUEST, f"未知级别：{'、'.join(bad)}")
        elif k == "resource_types":
            bad = [x for x in v if x not in RESOURCE_TYPES and x not in PLATFORM_TYPES]
            if bad:
                raise BizError(ErrorCode.BAD_REQUEST, f"未知资源类型：{'、'.join(bad)}")
        out[k] = v
    return out


def _group_by_code(db: Session, code: str) -> ContactGroup | None:
    return db.scalar(select(ContactGroup).where(ContactGroup.code == code))


def _check_route(db: Session, body: RouteIn) -> dict:
    if _group_by_code(db, body.group_code) is None:
        raise BizError(ErrorCode.BAD_REQUEST, f"联系组不存在：{body.group_code}")
    esc = []
    for e in body.escalation:
        if e.to != "leader" and _group_by_code(db, e.to) is None:
            raise BizError(ErrorCode.BAD_REQUEST, f"升级目标联系组不存在：{e.to}")
        bad = [x for x in e.severities if x not in SEVERITIES]
        if bad:
            raise BizError(ErrorCode.BAD_REQUEST, f"未知级别：{'、'.join(bad)}")
        esc.append({"after_min": e.after_min, "to": e.to, "severities": sorted(set(e.severities))})
    return {"name": body.name.strip(), "priority": body.priority, "match": _check_match(db, body.match),
            "group_code": body.group_code, "escalation": sorted(esc, key=lambda x: x["after_min"]),
            "continue_match": body.continue_match, "enabled": body.enabled}


def _route_out(db: Session, r: NotifyRoute, tm: dict, groups: dict[str, ContactGroup]) -> dict:
    g = groups.get(r.group_code)
    return {"id": r.id, "name": r.name, "priority": r.priority, "match": r.match, "group_code": r.group_code,
            "group_name": g.name if g else None, "member_count": len(g.member_ids or []) if g else 0,
            "tenant_names": [tm[t].short_name or tm[t].name for t in (r.match or {}).get("tenant_ids") or [] if t in tm],
            "escalation": r.escalation, "continue_match": r.continue_match, "enabled": r.enabled}


# ---------------- 路由 ----------------
@router.get("/routes", summary="通知路由（按优先级）")
def routes(db: Session = Depends(get_db)):
    tm = tenant_map(db)
    groups = {g.code: g for g in db.scalars(select(ContactGroup))}
    return ok([_route_out(db, r, tm, groups) for r in db.scalars(select(NotifyRoute).order_by(NotifyRoute.priority,
                                                                                             NotifyRoute.id))])


@router.post("/routes", summary="新增通知路由")
def create_route(body: RouteIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                 _: CurrentUser = Depends(manager)):
    r = NotifyRoute(**_check_route(db, body))
    db.add(r)
    db.flush()
    audit.log("notify.route.create", target_type="notify_route", target_id=r.id, target_name=r.name,
              detail={"match": r.match, "group": r.group_code, "priority": r.priority})
    return ok({"id": r.id})


@router.put("/routes/{rid}", summary="修改通知路由")
def update_route(rid: int, body: RouteIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                 _: CurrentUser = Depends(manager)):
    r = db.get(NotifyRoute, rid)
    if r is None:
        raise not_found("通知路由")
    data = _check_route(db, body)
    diff = {k: [getattr(r, k), v] for k, v in data.items() if getattr(r, k) != v}
    for k, v in data.items():
        setattr(r, k, v)
    audit.log("notify.route.update", target_type="notify_route", target_id=r.id, target_name=r.name,
              detail={"changes": diff})
    return ok({"id": r.id})


@router.delete("/routes/{rid}", summary="删除通知路由")
def delete_route(rid: int, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                 _: CurrentUser = Depends(manager)):
    r = db.get(NotifyRoute, rid)
    if r is None:
        raise not_found("通知路由")
    name = r.name
    db.delete(r)
    audit.log("notify.route.delete", target_type="notify_route", target_id=rid, target_name=name)
    return ok()


@router.post("/routes/reorder", summary="调整路由顺序（按传入顺序重排优先级 10、20、30 …）")
def reorder_routes(body: ReorderIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                   _: CurrentUser = Depends(manager)):
    rows = {r.id: r for r in db.scalars(select(NotifyRoute))}
    if sorted(body.ids) != sorted(rows):
        raise BizError(ErrorCode.BAD_REQUEST, "排序需要包含全部路由")
    for i, rid in enumerate(body.ids, 1):
        rows[rid].priority = i * 10
    audit.log("notify.route.reorder", target_type="notify_route", target_name="通知路由排序",
              detail={"order": [rows[i].name for i in body.ids]})
    return ok()


# ---------------- 联系组 ----------------
@router.get("/groups", summary="联系组（含成员、组长、被哪些路由使用）")
def groups(db: Session = Depends(get_db)):
    contacts = _contacts(db)
    used: dict[str, list[str]] = {}
    for r in db.scalars(select(NotifyRoute)):
        used.setdefault(r.group_code, []).append(r.name)
        for e in r.escalation or []:
            if e.get("to") and e["to"] != "leader":
                used.setdefault(e["to"], []).append(f"{r.name}（升级）")
    return ok([{"id": g.id, "code": g.code, "name": g.name, "member_ids": g.member_ids, "leader_ids": g.leader_ids,
                "members": _pick(contacts, g.member_ids or []), "leaders": _pick(contacts, g.leader_ids or []),
                "routes": used.get(g.code, [])} for g in db.scalars(select(ContactGroup).order_by(ContactGroup.id))])


def _check_members(db: Session, body: GroupIn) -> tuple[list[int], list[int]]:
    known = set(_contacts(db))
    ids = list(dict.fromkeys(body.member_ids))
    leaders = list(dict.fromkeys(body.leader_ids))
    if any(i not in known for i in ids + leaders):
        raise BizError(ErrorCode.BAD_REQUEST, "成员或组长中包含不存在的联系人")
    if not ids:
        raise BizError(ErrorCode.BAD_REQUEST, "联系组至少需要一个成员")
    return ids, leaders


@router.post("/groups", summary="新增联系组")
def create_group(body: GroupIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                 _: CurrentUser = Depends(manager)):
    if _group_by_code(db, body.code) is not None:
        raise BizError(ErrorCode.CONFLICT, f"联系组代码已存在：{body.code}", 409)
    ids, leaders = _check_members(db, body)
    g = ContactGroup(code=body.code, name=body.name.strip(), member_ids=ids, leader_ids=leaders)
    db.add(g)
    db.flush()
    audit.log("notify.group.create", target_type="contact_group", target_id=g.id, target_name=g.name,
              detail={"code": g.code, "members": len(ids), "leaders": len(leaders)})
    return ok({"id": g.id})


@router.put("/groups/{gid}", summary="修改联系组（代码不可修改）")
def update_group(gid: int, body: GroupIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                 _: CurrentUser = Depends(manager)):
    g = db.get(ContactGroup, gid)
    if g is None:
        raise not_found("联系组")
    if body.code != g.code:
        raise BizError(ErrorCode.BAD_REQUEST, "联系组代码创建后不能修改（路由按代码引用）")
    ids, leaders = _check_members(db, body)
    contacts = _contacts(db)
    names = lambda xs: [contacts[i].name for i in xs if i in contacts]  # noqa: E731
    diff: dict[str, list] = {}
    if g.name != body.name.strip():
        diff["名称"] = [g.name, body.name.strip()]
    if list(g.member_ids or []) != ids:
        diff["成员"] = [names(g.member_ids or []), names(ids)]
    if list(g.leader_ids or []) != leaders:
        diff["组长"] = [names(g.leader_ids or []), names(leaders)]
    g.name, g.member_ids, g.leader_ids = body.name.strip(), ids, leaders
    audit.log("notify.group.update", target_type="contact_group", target_id=g.id, target_name=g.name,
              detail={"changes": diff})
    return ok({"id": g.id})


@router.delete("/groups/{gid}", summary="删除联系组（仍被路由使用时不能删除）")
def delete_group(gid: int, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                 _: CurrentUser = Depends(manager)):
    g = db.get(ContactGroup, gid)
    if g is None:
        raise not_found("联系组")
    users = [r.name for r in db.scalars(select(NotifyRoute)) if r.group_code == g.code
             or any(e.get("to") == g.code for e in r.escalation or [])]
    if users:
        raise BizError(ErrorCode.CONFLICT, f"联系组仍被路由使用：{'、'.join(users)}", 409, {"routes": users})
    name = g.name
    db.delete(g)
    audit.log("notify.group.delete", target_type="contact_group", target_id=gid, target_name=name)
    return ok()


# ---------------- 联系人 ----------------
@router.get("/contacts", summary="联系人（含所在联系组）")
def contacts(db: Session = Depends(get_db)):
    groups = list(db.scalars(select(ContactGroup).order_by(ContactGroup.id)))
    return ok([{"id": c.id, "name": c.name, "email": c.email, "phone": c.phone, "enabled": c.enabled,
                "groups": [{"code": g.code, "name": g.name, "leader": c.id in (g.leader_ids or [])}
                           for g in groups if c.id in (g.member_ids or []) or c.id in (g.leader_ids or [])]}
               for c in db.scalars(select(Contact).order_by(Contact.id))])


def _dup_email(db: Session, email: str, exclude: int | None = None) -> None:
    q = select(Contact).where(Contact.email == email)
    if exclude is not None:
        q = q.where(Contact.id != exclude)
    if db.scalar(q) is not None:
        raise BizError(ErrorCode.CONFLICT, f"邮箱已存在：{email}", 409)


@router.post("/contacts", summary="新增联系人")
def create_contact(body: ContactIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                   _: CurrentUser = Depends(manager)):
    _dup_email(db, body.email)
    c = Contact(name=body.name.strip(), email=body.email, phone=(body.phone or "").strip() or None,
                enabled=body.enabled)
    db.add(c)
    db.flush()
    audit.log("notify.contact.create", target_type="contact", target_id=c.id, target_name=c.name,
              detail={"email": c.email})
    return ok({"id": c.id})


@router.put("/contacts/{cid}", summary="修改联系人（停用后不再收到通知）")
def update_contact(cid: int, body: ContactIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                   _: CurrentUser = Depends(manager)):
    c = db.get(Contact, cid)
    if c is None:
        raise not_found("联系人")
    _dup_email(db, body.email, exclude=cid)
    data = {"name": body.name.strip(), "email": body.email, "phone": (body.phone or "").strip() or None,
            "enabled": body.enabled}
    diff = {k: [getattr(c, k), v] for k, v in data.items() if getattr(c, k) != v}
    for k, v in data.items():
        setattr(c, k, v)
    audit.log("notify.contact.update", target_type="contact", target_id=c.id, target_name=c.name,
              detail={"changes": diff})
    return ok({"id": c.id})


@router.delete("/contacts/{cid}", summary="删除联系人（同时从所在联系组移除；不能删除某个组的唯一成员）")
def delete_contact(cid: int, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                   _: CurrentUser = Depends(manager)):
    c = db.get(Contact, cid)
    if c is None:
        raise not_found("联系人")
    affected = []
    for g in db.scalars(select(ContactGroup)):
        if cid in (g.member_ids or []) and len(g.member_ids) == 1:
            raise BizError(ErrorCode.CONFLICT, f"{c.name} 是联系组“{g.name}”的唯一成员，请先调整该组", 409)
        if cid in (g.member_ids or []) or cid in (g.leader_ids or []):
            g.member_ids = [i for i in g.member_ids or [] if i != cid]
            g.leader_ids = [i for i in g.leader_ids or [] if i != cid]
            affected.append(g.name)
    name = c.name
    db.delete(c)
    audit.log("notify.contact.delete", target_type="contact", target_id=cid, target_name=name,
              detail={"removed_from": affected})
    return ok({"removed_from": affected})


# ---------------- 模拟 / 测试 ----------------
@router.post("/simulate", summary="路由模拟：输入租户 / 服务 / 级别，查看会命中哪些路由、发给谁、何时升级、是否被静默")
def simulate(body: SimulateIn, db: Session = Depends(get_db)):
    if body.severity not in SEVERITIES:
        raise BizError(ErrorCode.BAD_REQUEST, f"未知级别：{body.severity}")
    now = utcnow()
    alarm = AlarmEvent(tenant_id=body.tenant_id, service=body.service, resource_type=body.resource_type or "",
                       severity=body.severity, rule_id=body.rule_id, resource_name=body.resource_name,
                       fingerprint="simulate", status="FIRING", title="路由模拟", check_item="simulate",
                       first_seen=now, last_seen=now, detail={})
    contacts = _contacts(db)
    groups = {g.code: g for g in db.scalars(select(ContactGroup))}
    steps: list[dict] = []
    emails: list[str] = []
    stopped = False
    for r in db.scalars(select(NotifyRoute).order_by(NotifyRoute.priority, NotifyRoute.id)):
        matched = r.enabled and not stopped and route_matches(r.match or {}, alarm)
        g = groups.get(r.group_code)
        members = [c for c in _pick(contacts, (g.member_ids if g else []) or []) if c["enabled"]]
        steps.append({"id": r.id, "name": r.name, "priority": r.priority, "enabled": r.enabled,
                      "skipped": stopped, "matched": matched, "group_name": g.name if g else r.group_code,
                      "recipients": members if matched else [], "continue_match": r.continue_match})
        if matched:
            emails.extend(m["email"] for m in members if m["email"] not in emails)
            if not r.continue_match:
                stopped = True
    silence = silence_service.find_silence(db, alarm, now)
    platform_fallback = None
    if not emails and body.service == "platform":
        from app.services.platform_watch import fallback_recipients
        platform_fallback = fallback_recipients(db)
    return ok({"steps": steps, "recipients": emails, "escalation": escalation_plan(db, alarm),
               "silence": {"id": silence.id, "name": silence.name, "kind": silence.kind} if silence else None,
               "platform_fallback": platform_fallback})


@router.get("/smtp", summary="SMTP 配置（只读，来自环境变量；不返回口令）")
def smtp_info():
    s = get_settings()
    return ok({"host": s.smtp_host, "port": s.smtp_port, "starttls": s.smtp_starttls, "auth": bool(s.smtp_user),
               "sender": s.smtp_sender, "reply_to": s.smtp_reply_to, "env_tag": s.env_tag, "base_url": s.base_url})


@router.post("/test-mail", summary="发送测试邮件（同步发送，用于验证 SMTP 配置与收件人地址）")
def test_mail(body: TestMailIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
              cu: CurrentUser = Depends(manager)):
    to = [x.strip() for x in body.to if x.strip()]
    bad = [x for x in to if not EMAIL.match(x)]
    if bad or not to:
        raise BizError(ErrorCode.BAD_REQUEST, f"邮箱格式不正确：{'、'.join(bad) or '未填写收件人'}")
    s = get_settings()
    at = utcnow().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    text = (f"这是一封来自 HCS 巡检平台的测试邮件。\n发送人：{cu.display_name}\n时间：{at}\n"
            f"SMTP：{s.smtp_host}:{s.smtp_port}\n收到此邮件说明告警通知可以正常送达。")
    html = "<br>".join(text.split("\n"))
    msg = build_message(subject=f"【{s.env_tag}】HCS 巡检平台测试邮件", html=f"<p>{html}</p>", text=text,
                        recipients=to, message_id=f"<test-{int(time.time() * 1000)}@hcs-inspector>")
    t0 = time.monotonic()
    try:
        send(msg)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"[:300]
        audit.log("notify.test_mail", target_type="smtp", target_name=f"{s.smtp_host}:{s.smtp_port}", result="FAILED",
                  detail={"to": to, "error": err})
        raise BizError(ErrorCode.HCS_UPSTREAM, f"测试邮件发送失败：{err}", 502) from e
    ms = int((time.monotonic() - t0) * 1000)
    audit.log("notify.test_mail", target_type="smtp", target_name=f"{s.smtp_host}:{s.smtp_port}",
              detail={"to": to, "elapsed_ms": ms})
    return ok({"sent_to": to, "elapsed_ms": ms})


# ---------------- 记录 / 预览 ----------------
@router.get("/notifications", summary="最近通知记录（Outbox，按租户范围过滤）")
def notifications(status: str | None = None, limit: int = Query(50, le=200), db: Session = Depends(get_db),
                  scope: Scope = Depends(get_scope)):
    q = select(Notification, AlarmEvent.severity, AlarmEvent.tenant_id).join(
        AlarmEvent, AlarmEvent.id == Notification.alarm_id, isouter=True)
    if status:
        q = q.where(Notification.status == status)
    q = scope.apply(q, AlarmEvent.tenant_id, include_global=True)
    rows = db.execute(q.order_by(Notification.created_at.desc()).limit(limit)).all()
    return ok([{**NotificationOut.model_validate(n).model_dump(mode="json"), "alarm_id": n.alarm_id, "severity": sev,
                "tenant_id": tid, "retry": n.retry, "created_at": n.created_at} for n, sev, tid in rows])


@router.get("/preview/{alarm_id}", summary="邮件预览（渲染真实模板）")
def preview(alarm_id: int, kind: str = "FIRING", db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    alarm = get_alarm(db, alarm_id)
    scope.ensure(alarm.tenant_id, "告警")
    recipients, route_names = resolve_recipients(db, alarm)
    subject, html, text = render_alarm_mail(db, alarm, kind, recipients[0] if recipients else "", route_names)
    return ok({"subject": subject, "html": html, "text": text, "recipients": recipients, "routes": route_names})
