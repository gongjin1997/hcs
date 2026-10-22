"""告警静默与维护窗口：列表、创建（保存前可预览命中的告警）、修改、取消，以及告警详情里的“快速静默”。

- 查看：alarm:view；创建 / 修改 / 取消：silence:manage
- 数据范围：只有全部租户权限的用户可以创建不限租户的静默；部分租户用户的静默必须限定在自己的租户内，
  且只能看到、修改与自己租户相关的静默（不限租户的静默只读可见）
- 命中静默的告警照常记录（状态“已抑制”），不发通知；静默结束后仍未恢复的告警自动转回“告警中”并补发通知
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_auditor
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.response import BizError, ErrorCode, not_found, ok
from app.models import AlarmEvent, Silence
from app.models.base import utcnow
from app.schemas.alarm import AlarmOut
from app.services import alarm_service, silence_service
from app.services.tenancy import tenant_map

from .auth import display_name

router = APIRouter(prefix="/silences", tags=["静默与维护窗口"])
MANAGE = require_perm("silence:manage")


class SilenceIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: str = Field("SILENCE", pattern="^(SILENCE|MAINTENANCE)$")
    matchers: dict
    starts_at: datetime | None = None          # 为空 = 立即开始
    ends_at: datetime
    recurrence: dict | None = None             # {"weekdays": [4], "start": "22:00", "end": "02:00"}
    reason: str = Field("", max_length=500)


class PreviewIn(BaseModel):
    matchers: dict


class QuickSilenceIn(BaseModel):
    minutes: int = Field(120, ge=10, le=7 * 24 * 60)
    target: str = Field("alarm", pattern="^(alarm|resource|rule)$")   # 只静默这条告警 / 该资源全部告警 / 该租户下这条规则
    reason: str = Field("", max_length=500)


def _names(db: Session) -> dict[int, str]:
    return {t.id: t.short_name or t.name for t in tenant_map(db).values()}


def _visible(scope: Scope, s: Silence) -> bool:
    tids = (s.matchers or {}).get("tenant_ids") or []
    return scope.tenant_ids is None or not tids or bool(set(tids) & set(scope.tenant_ids))


def _editable(scope: Scope, s: Silence) -> bool:
    """全部租户权限的用户可以修改任何静默；部分租户用户只能修改完全落在自己租户内的静默。"""
    if scope.full:
        return True
    tids = (s.matchers or {}).get("tenant_ids") or []
    return bool(tids) and set(tids) <= set(scope.tenant_ids or [])


def _check_scope(scope: Scope, matchers: dict) -> dict:
    """数据范围：不指定租户时自动限定为当前可见的租户（顶部切换到某个租户时 = 只静默该租户）；指定了范围外的租户时拒绝。"""
    m = dict(matchers)
    if scope.tenant_ids is not None:
        tids = [int(t) for t in (m.get("tenant_ids") or [])] or list(scope.tenant_ids)
        if any(t not in scope.tenant_ids for t in tids):
            raise BizError(ErrorCode.FORBIDDEN, "无权静默其他租户的告警", 403)
        m["tenant_ids"] = tids
    return m


def _out(db: Session, s: Silence, now: datetime, scope: Scope, names: dict[int, str]) -> dict:
    nxt = silence_service.next_window(s, now)
    suppressed = db.scalars(select(AlarmEvent.id).where(AlarmEvent.status == "SUPPRESSED",
                                                        AlarmEvent.silence_id == s.id)).all()
    return {"id": s.id, "name": s.name, "kind": s.kind, "matchers": s.matchers, "recurrence": s.recurrence or {},
            "starts_at": s.starts_at, "ends_at": s.ends_at, "reason": s.reason, "created_by": s.created_by,
            "created_at": s.created_at, "updated_at": s.updated_at, "cancelled_by": s.cancelled_by,
            "cancelled_at": s.cancelled_at, "status": silence_service.status_of(s, now),
            "next_window": {"start": nxt[0], "end": nxt[1]} if nxt else None,
            "describe": silence_service.describe(s.matchers or {}, names),
            "suppressed_now": len(suppressed), "suppressed_total": s.suppressed_total or 0,
            "editable": _editable(scope, s)}


def _get(db: Session, scope: Scope, sid: int) -> Silence:
    s = db.get(Silence, sid)
    if s is None or not _visible(scope, s):
        raise not_found("静默")
    return s


def _apply(db: Session, s: Silence, body: SilenceIn, scope: Scope, now: datetime) -> None:
    rec = silence_service.normalize_recurrence(body.recurrence)
    starts = body.starts_at or now
    silence_service.validate_window(starts, body.ends_at, rec)
    s.name, s.kind, s.reason = body.name.strip(), body.kind, body.reason.strip()
    s.matchers = silence_service.normalize_matchers(_check_scope(scope, body.matchers))
    s.recurrence, s.starts_at, s.ends_at = rec, starts, body.ends_at


@router.get("", summary="静默与维护窗口列表")
def list_silences(status: str = Query("current", pattern="^(current|active|pending|expired|cancelled|all)$"),
                  db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                  _: CurrentUser = Depends(require_perm("alarm:view"))):
    now = utcnow()
    names = _names(db)
    rows = [s for s in db.scalars(select(Silence).order_by(Silence.starts_at.desc(), Silence.id.desc()))
            if _visible(scope, s)]
    counts = {k: 0 for k in ("ACTIVE", "WAITING", "PENDING", "EXPIRED", "CANCELLED")}
    for s in rows:
        counts[silence_service.status_of(s, now)] += 1
    want = {"current": ("ACTIVE", "WAITING", "PENDING"), "active": ("ACTIVE", "WAITING"), "pending": ("PENDING",),
            "expired": ("EXPIRED",), "cancelled": ("CANCELLED",)}.get(status)
    items = [_out(db, s, now, scope, names) for s in rows if want is None or silence_service.status_of(s, now) in want]
    return ok({"items": items, "counts": counts})


@router.post("/preview", summary="预览：当前会被命中的活动告警（保存前确认，防止误伤核心告警）")
def preview(body: PreviewIn, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
            _: CurrentUser = Depends(MANAGE)):
    res = silence_service.preview(db, _check_scope(scope, body.matchers), scope.tenant_ids, limit=50)
    return ok({**res, "items": [AlarmOut.model_validate(a) for a in res["items"]]})


@router.post("", summary="新建静默 / 维护窗口（生效中的会立即抑制已在告警中的匹配告警）")
def create(body: SilenceIn, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
           audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(MANAGE)):
    now = utcnow()
    s = Silence(created_by=display_name(db, cu), created_at=now)
    _apply(db, s, body, scope, now)
    db.add(s)
    db.flush()
    res = silence_service.sweep(db, now, only=s)
    names = _names(db)
    audit.log("silence.create", target_type="silence", target_id=s.id, target_name=s.name, tenant_id=scope.single,
              detail={"kind": s.kind, "matchers": silence_service.describe(s.matchers, names),
                      "window": f"{s.starts_at.isoformat()} ~ {s.ends_at.isoformat()}", "recurrence": s.recurrence,
                      "suppressed": res["suppressed"]})
    return ok({**_out(db, s, now, scope, names), "suppressed": res["suppressed"]})


@router.put("/{sid}", summary="修改静默 / 维护窗口（已取消、已结束的不能修改）")
def update(sid: int, body: SilenceIn, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
           audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(MANAGE)):
    now = utcnow()
    s = _get(db, scope, sid)
    if not _editable(scope, s):
        raise BizError(ErrorCode.FORBIDDEN, "该静默包含其他租户，无权修改", 403)
    if silence_service.status_of(s, now) in ("EXPIRED", "CANCELLED"):
        raise BizError(ErrorCode.CONFLICT, "已结束或已取消的静默不能修改，请新建", 409)
    before = {"name": s.name, "matchers": dict(s.matchers or {}), "ends_at": s.ends_at.isoformat(),
              "recurrence": dict(s.recurrence or {})}
    _apply(db, s, body, scope, now)
    db.flush()
    released = silence_service.release(db, s, now, display_name(db, cu))
    res = silence_service.sweep(db, now, only=s)
    names = _names(db)
    audit.log("silence.update", target_type="silence", target_id=s.id, target_name=s.name, tenant_id=scope.single,
              detail={"before": before, "after": {"name": s.name, "matchers": s.matchers,
                                                  "ends_at": s.ends_at.isoformat(), "recurrence": s.recurrence},
                      "released": released, "suppressed": res["suppressed"]})
    return ok({**_out(db, s, now, scope, names), "released": released, "suppressed": res["suppressed"]})


@router.post("/{sid}/cancel", summary="取消静默（被抑制且仍未恢复的告警转回“告警中”并补发通知）")
def cancel(sid: int, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
           audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(MANAGE)):
    now = utcnow()
    s = _get(db, scope, sid)
    if not _editable(scope, s):
        raise BizError(ErrorCode.FORBIDDEN, "该静默包含其他租户，无权取消", 403)
    if s.cancelled_at is not None:
        raise BizError(ErrorCode.CONFLICT, "该静默已取消", 409)
    operator = display_name(db, cu)
    s.cancelled_at, s.cancelled_by = now, operator
    released = silence_service.release(db, s, now, operator)
    audit.log("silence.cancel", target_type="silence", target_id=s.id, target_name=s.name, tenant_id=scope.single,
              detail={"released": released})
    return ok({**_out(db, s, now, scope, _names(db)), "released": released})


quick_router = APIRouter(prefix="/alarms", tags=["告警"])


@quick_router.post("/{alarm_id}/silence", summary="快速静默：只这条告警 / 该资源全部告警 / 该租户下这条规则，静默 N 分钟")
def quick_silence(alarm_id: int, body: QuickSilenceIn, db: Session = Depends(get_db),
                  scope: Scope = Depends(get_scope), audit: Auditor = Depends(get_auditor),
                  cu: CurrentUser = Depends(MANAGE)):
    alarm = alarm_service.get_alarm(db, alarm_id)
    scope.ensure(alarm.tenant_id, "告警")
    if alarm.status in ("RESOLVED", "CLOSED"):
        raise BizError(ErrorCode.CONFLICT, "告警已恢复或已关闭，无需静默", 409)
    tenant = [alarm.tenant_id] if alarm.tenant_id is not None else []
    if body.target == "resource" and alarm.resource_ref is not None:
        m: dict = {"resource_refs": [alarm.resource_ref], "tenant_ids": tenant}
        label = f"{alarm.resource_name} 的全部告警"
    elif body.target == "rule":
        m = {"rule_ids": [alarm.rule_id], "tenant_ids": tenant}
        label = f"规则 {alarm.rule_id}"
    else:
        m = {"fingerprints": [alarm.fingerprint], "tenant_ids": tenant}
        label = f"{alarm.resource_name} · {alarm.title}"
    now = utcnow()
    hours = body.minutes / 60
    s = Silence(name=f"快速静默：{label}"[:128], kind="SILENCE", matchers=silence_service.normalize_matchers(m),
                recurrence={}, starts_at=now, ends_at=now + timedelta(minutes=body.minutes),
                reason=body.reason.strip() or f"告警 #{alarm.id} 处理中，静默 {hours:g} 小时",
                created_by=display_name(db, cu), created_at=now)
    db.add(s)
    db.flush()
    if alarm.status == "FIRING":
        silence_service.suppress(db, alarm, s, now)
    res = silence_service.sweep(db, now, only=s)
    audit.log("alarm.silence", target_type="alarm", target_id=alarm.id,
              target_name=f"{alarm.resource_name} · {alarm.title}", tenant_id=alarm.tenant_id,
              detail={"silence_id": s.id, "minutes": body.minutes, "target": body.target, "reason": s.reason})
    return ok({"silence": _out(db, s, now, scope, _names(db)),
               "suppressed": res["suppressed"] + (1 if alarm.status == "SUPPRESSED" else 0)})
