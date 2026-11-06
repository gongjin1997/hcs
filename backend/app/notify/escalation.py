"""未认领升级：告警保持 FIRING（无人认领）超过路由里配置的 after_min 分钟，按级别再通知组长或指定联系组。

路由的 escalation 配置示例：[{"after_min": 15, "to": "leader", "severities": ["P1", "P2"]}]
- to = leader：通知该路由联系组的组长；to = 其他联系组代码：通知该组全部成员
- 每一级只升级一次（以告警日志里的 ESCALATE 条数判断），认领 / 恢复后不再升级
由 notifier 进程每分钟检查一次（app.workers.notifier）。
"""
import logging
from datetime import datetime, timedelta
from time import perf_counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AlarmEvent, AlarmEventLog, ContactGroup, Notification, NotifyRoute
from app.models.base import utcnow
from app.notify.render import build_subject
from app.notify.routing import contact_allows, route_matches
from app.services.identity import contact_users

log = logging.getLogger(__name__)


def escalation_plan(db: Session, alarm: AlarmEvent) -> list[dict]:
    """命中的路由中适用于该告警级别的升级规则，按 after_min 升序：[{route, after_min, emails, names, note}]。"""
    plans: list[dict] = []
    users = contact_users(db)
    routes = db.scalars(select(NotifyRoute).where(NotifyRoute.enabled.is_(True))
                        .order_by(NotifyRoute.priority, NotifyRoute.id))
    for r in routes:
        if not route_matches(r.match or {}, alarm):
            continue
        group = db.scalar(select(ContactGroup).where(ContactGroup.code == r.group_code))
        for esc in r.escalation or []:
            if esc.get("severities") and alarm.severity not in esc["severities"]:
                continue
            to = esc.get("to") or "leader"
            target = group if to == "leader" else db.scalar(select(ContactGroup).where(ContactGroup.code == to))
            if target is None:
                continue
            ids = list((target.leader_ids if to == "leader" else target.member_ids) or [])
            contacts = [users[cid] for cid in ids if contact_allows(users.get(cid), alarm.tenant_id)]
            if not contacts:
                continue
            names = "、".join(c.display_name for c in contacts)
            after = int(esc.get("after_min", 15))
            who = f"组长 {names}" if to == "leader" else f"{target.name}（{names}）"
            plans.append({"route": r.name, "route_id": r.id, "after_min": after, "emails": [c.email for c in contacts],
                          "names": [c.display_name for c in contacts], "note": f"{after} 分钟未认领，升级通知{who}"})
        if not r.continue_match:
            break
    return sorted(plans, key=lambda p: p["after_min"])


def escalate(db: Session, alarm: AlarmEvent, plan: dict, at: datetime, status: str = "PENDING") -> Notification:
    n = Notification(alarm_id=alarm.id, kind="ESCALATE", channel="SMTP", recipients=plan["emails"],
                     subject=build_subject(alarm, "ESCALATE", at), status=status, created_at=at)
    db.add(n)
    db.flush()
    db.add(AlarmEventLog(alarm_id=alarm.id, action="ESCALATE", from_status="FIRING", to_status="FIRING",
                         operator="system", note=f"{plan['note']}；路由：{plan['route']}（#{plan.get('route_id', '-')}）；"
                         f"通知 #{n.id} 已入队，尚未发送；收件人：{'、'.join(plan['emails'])}", at=at))
    alarm.notify_seq += 1
    return n


def escalate_unacked(db: Session, now: datetime | None = None) -> int:
    """检查所有未认领告警，到点的升级通知写入 Outbox（由 dispatcher 发送）。返回本次升级条数。"""
    now = now or utcnow()
    started = perf_counter()
    count = 0
    query = select(AlarmEvent).where(AlarmEvent.status == "FIRING").order_by(AlarmEvent.id)
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    for alarm in db.scalars(query).all():
        plans = escalation_plan(db, alarm)
        if not plans:
            continue
        reopened = db.scalar(select(AlarmEventLog).where(AlarmEventLog.alarm_id == alarm.id,
                             AlarmEventLog.action == "REOPEN")
                             .order_by(AlarmEventLog.at.desc(), AlarmEventLog.id.desc()).limit(1))
        since = reopened.at if reopened is not None else alarm.first_seen
        done_query = select(func.count()).select_from(AlarmEventLog).where(
            AlarmEventLog.alarm_id == alarm.id, AlarmEventLog.action == "ESCALATE")
        if reopened is not None:
            done_query = done_query.where(AlarmEventLog.id > reopened.id)
        done = db.scalar(done_query) or 0
        for plan in plans[done:]:
            if now - since < timedelta(minutes=plan["after_min"]):
                break
            escalate(db, alarm, plan, now)
            count += 1
    db.commit()
    if count:
        log.info("notification_escalation_finished", extra={"event": "notification_escalation_finished",
                 "escalation_count": count, "cost_ms": round((perf_counter() - started) * 1000, 2)})
    return count
