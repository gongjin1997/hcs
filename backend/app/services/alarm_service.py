"""告警服务：列表 / 详情 / 认领 / 关闭，以及巡检流水线调用的状态机落库（与通知 Outbox 同事务）。"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.collectors.base import Observation
from app.core.events import publish
from app.core.response import BizError, ErrorCode, not_found
from app.models import AlarmEvent, AlarmEventLog, AlarmRule, Notification, Resource
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow
from app.notify.render import build_subject
from app.notify.routing import resolve_recipients
from app.rules.evaluator import Decision
from app.rules.expr import ExprError, evaluate
from app.rules.model import Rule, render_title
from app.schemas.alarm import AlarmOut

from . import silence_service
from .catalog import STATUS_LABEL

REOPEN_WINDOW = timedelta(minutes=30)     # 恢复后 30 分钟内再次触发：重开原告警，避免刷屏
STATUS_GROUPS: dict[str, tuple[str, ...] | None] = {
    "active": ACTIVE_STATUSES, "firing": ("FIRING",), "acked": ("ACKED",), "suppressed": ("SUPPRESSED",),
    "resolved": ("RESOLVED", "CLOSED"), "all": None,
}
log = logging.getLogger(__name__)


# ---------------- 查询 ----------------
def query_alarms(db: Session, *, status_group: str = "active", severities: list[str] | None = None,
                 service: str | None = None, resource_type: str | None = None, resource_ref: int | None = None,
                 keyword: str | None = None, page: int = 1, page_size: int = 20,
                 tenant_ids: list[int] | None = None) -> tuple[list[AlarmEvent], int]:
    q = select(AlarmEvent)
    if tenant_ids is not None:                     # 数据范围：用户可见租户 ∩ 顶部租户切换
        q = q.where(AlarmEvent.tenant_id.in_(tenant_ids))
    statuses = STATUS_GROUPS.get(status_group, ACTIVE_STATUSES)
    if statuses:
        q = q.where(AlarmEvent.status.in_(statuses))
    if severities:
        q = q.where(AlarmEvent.severity.in_(severities))
    if service:
        q = q.where(AlarmEvent.service == service)
    if resource_type:
        q = q.where(AlarmEvent.resource_type == resource_type)
    if resource_ref:
        q = q.where(AlarmEvent.resource_ref == resource_ref)
    if keyword:
        like = f"%{keyword.strip()}%"
        q = q.where(or_(AlarmEvent.title.ilike(like), AlarmEvent.resource_name.ilike(like),
                        AlarmEvent.rule_id.ilike(like)))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    order = ((AlarmEvent.severity, AlarmEvent.last_seen.desc()) if status_group != "resolved"
             else (AlarmEvent.resolved_at.desc(),))
    items = db.scalars(q.order_by(*order).offset((page - 1) * page_size).limit(page_size)).all()
    return list(items), total


def stats(db: Session, tenant_ids: list[int] | None = None) -> dict:
    out = {k: 0 for k in ("FIRING", "ACKED", "SUPPRESSED", "P1", "P2", "P3", "P4")}
    q = select(AlarmEvent.status, AlarmEvent.severity, func.count()).where(AlarmEvent.status.in_(ACTIVE_STATUSES))
    if tenant_ids is not None:
        q = q.where(AlarmEvent.tenant_id.in_(tenant_ids))
    rows = db.execute(q.group_by(AlarmEvent.status, AlarmEvent.severity))
    for status, sev, n in rows:
        out[status] += n
        out[sev] += n
    return out


def get_alarm(db: Session, alarm_id: int) -> AlarmEvent:
    alarm = db.get(AlarmEvent, alarm_id)
    if alarm is None:
        raise not_found("告警")
    return alarm


def alarm_detail(db: Session, alarm: AlarmEvent) -> dict:
    rule = db.get(AlarmRule, alarm.rule_id)
    logs = db.scalars(select(AlarmEventLog).where(AlarmEventLog.alarm_id == alarm.id).order_by(AlarmEventLog.at))
    notes = db.scalars(select(Notification).where(Notification.alarm_id == alarm.id).order_by(Notification.id))
    return {**AlarmOut.model_validate(alarm).model_dump(), "fingerprint": alarm.fingerprint, "detail": alarm.detail,
            "runbook": rule.runbook if rule else None, "logs": list(logs), "notifications": list(notes)}


# ---------------- 人工操作 ----------------
def _log(db: Session, alarm: AlarmEvent, action: str, frm: str | None, to: str | None, operator: str = "system",
         note: str | None = None, at: datetime | None = None) -> None:
    db.add(AlarmEventLog(alarm_id=alarm.id, action=action, from_status=frm, to_status=to, operator=operator,
                         note=note, at=at or utcnow()))


def _event(alarm: AlarmEvent, action: str) -> dict:
    return {"type": action, "alarm": AlarmOut.model_validate(alarm).model_dump(mode="json")}


def ack(db: Session, alarm_id: int, operator: str, note: str | None = None) -> AlarmEvent:
    alarm = get_alarm(db, alarm_id)
    if alarm.status != "FIRING":
        raise BizError(ErrorCode.CONFLICT, f"告警当前为“{STATUS_LABEL.get(alarm.status)}”，不可认领", 409)
    alarm.status, alarm.ack_by, alarm.ack_at = "ACKED", operator, utcnow()
    _log(db, alarm, "ACK", "FIRING", "ACKED", operator, note)
    db.commit()
    publish(_event(alarm, "ACK"))
    return alarm


def batch_ack(db: Session, ids: list[int], operator: str, note: str | None = None,
              tenant_ids: list[int] | None = None) -> list[AlarmEvent]:
    """只认领范围内、仍为“告警中”的告警，返回实际认领的告警。"""
    q = select(AlarmEvent).where(AlarmEvent.id.in_(ids), AlarmEvent.status == "FIRING")
    if tenant_ids is not None:
        q = q.where(AlarmEvent.tenant_id.in_(tenant_ids))
    done = []
    for alarm in db.scalars(q):
        alarm.status, alarm.ack_by, alarm.ack_at = "ACKED", operator, utcnow()
        _log(db, alarm, "ACK", "FIRING", "ACKED", operator, note or "批量认领")
        done.append(alarm)
    db.commit()
    for alarm in done:
        publish(_event(alarm, "ACK"))
    return done


def close(db: Session, alarm_id: int, operator: str, note: str | None = None) -> AlarmEvent:
    alarm = get_alarm(db, alarm_id)
    if alarm.status == "CLOSED":
        raise BizError(ErrorCode.CONFLICT, "告警已关闭", 409)
    prev, alarm.status = alarm.status, "CLOSED"
    alarm.resolved_at = alarm.resolved_at or utcnow()
    _log(db, alarm, "CLOSE", prev, "CLOSED", operator, note or "人工关闭")
    db.commit()
    publish(_event(alarm, "CLOSE"))
    return alarm


# ---------------- 流水线：状态机落库 ----------------
def enqueue_notification(db: Session, alarm: AlarmEvent, kind: str, now: datetime,
                         recipients: list[str] | None = None) -> Notification | None:
    """按通知路由写入 Outbox（recipients 指定时直接使用，例如平台告警的兜底收件人）。"""
    if recipients is None:
        recipients, _ = resolve_recipients(db, alarm)
    if not recipients:
        log.warning("alarm %s (%s) matched no notify route", alarm.id, alarm.rule_id)
        return None
    alarm.notify_seq += 1
    n = Notification(alarm_id=alarm.id, kind=kind, recipients=recipients, subject=build_subject(alarm, kind, now),
                     status="PENDING", created_at=now)
    db.add(n)
    return n


def _recover_value(rule: Rule, facts: dict) -> str | None:
    if rule.kind != "threshold":
        return None
    try:
        v = evaluate(rule.expr["value"], facts)
    except ExprError:
        return None
    return f"{round(v, 1) if isinstance(v, float) else v}{rule.expr.get('unit', '')}"


def apply_decision(db: Session, *, rule: Rule, decision: Decision, fp: str, resource: Resource, obs: Observation,
                   facts: dict, now: datetime, active: AlarmEvent | None, extra: dict | None = None) -> dict | None:
    m, action = decision.match, decision.action
    if action == "FIRE" and m:
        alarm = db.scalar(select(AlarmEvent).where(AlarmEvent.fingerprint == fp, AlarmEvent.status == "RESOLVED",
                                                   AlarmEvent.resolved_at >= now - REOPEN_WINDOW)
                          .order_by(AlarmEvent.resolved_at.desc()).limit(1))
        if alarm:
            alarm.status, alarm.resolved_at, alarm.reopen_count = "FIRING", None, alarm.reopen_count + 1
            alarm.severity, alarm.title, alarm.last_seen = m.severity, m.title, now
            alarm.current_value, alarm.threshold_text, alarm.count = m.current_value, m.threshold_text, alarm.count + 1
            _log(db, alarm, "REOPEN", "RESOLVED", "FIRING", note="恢复后 30 分钟内再次触发，重开原告警", at=now)
        else:
            alarm = AlarmEvent(
                fingerprint=fp, rule_id=rule.id, resource_ref=resource.id, tenant_id=resource.tenant_id,
                service=resource.service,
                resource_type=resource.resource_type, resource_name=resource.name, sub_object=obs.sub_object,
                severity=m.severity, status="FIRING", title=m.title, check_item=rule.check_item,
                current_value=m.current_value, threshold_text=m.threshold_text, owner=resource.owner,
                first_seen=now, last_seen=now,
                detail={"facts": facts, "resource_id": resource.resource_id, "critical": resource.critical,
                        "workspace": resource.attrs.get("workspace"), **(extra or {})})
            db.add(alarm)
            db.flush()
            _log(db, alarm, "FIRE", None, "FIRING", note=f"{rule.name}：{m.title}", at=now)
        silence = silence_service.find_silence(db, alarm, now)
        if silence is not None:                       # 维护窗口 / 静默期间：照常记录，不发通知
            silence_service.suppress(db, alarm, silence, now)
        else:
            enqueue_notification(db, alarm, "FIRING", now)
        return _event(alarm, "FIRE")
    if active is None:
        return None
    active.last_seen = now
    if action in ("UPGRADE", "DOWNGRADE") and m:
        prev = active.severity
        active.severity, active.title = m.severity, m.title
        active.current_value, active.threshold_text, active.count = m.current_value, m.threshold_text, active.count + 1
        _log(db, active, action, active.status, active.status, note=f"{prev} → {m.severity}（{m.title}）", at=now)
        if action == "UPGRADE" and active.status != "SUPPRESSED":
            enqueue_notification(db, active, "UPGRADE", now)
        return _event(active, action)
    if action == "REFRESH":
        active.count += 1
        if m:
            active.title, active.current_value = m.title, m.current_value
        if rule.repeat_interval_sec and active.status == "FIRING":    # 未认领的告警按间隔重复提醒
            last = db.scalar(select(func.max(Notification.created_at)).where(Notification.alarm_id == active.id))
            if last is None or (now - last).total_seconds() >= rule.repeat_interval_sec:
                enqueue_notification(db, active, "REPEAT", now)
        return None
    if action == "RESOLVE":
        prev = active.status
        active.status, active.resolved_at = "RESOLVED", now
        active.current_value = _recover_value(rule, facts) or active.current_value
        if rule.kind == "threshold":
            tpl = (rule.expr.get("title") or rule.name).split("（")[0]     # 恢复后去掉阈值部分，只保留当前值
            active.title = render_title(tpl, facts, value=active.current_value or "-")
        _log(db, active, "RESOLVE", prev, "RESOLVED", note="满足恢复条件，自动恢复", at=now)
        if prev != "SUPPRESSED":                      # 静默期间没发过告警通知，恢复也不再通知
            enqueue_notification(db, active, "RESOLVED", now)
        return _event(active, "RESOLVE")
    return None
