from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AlarmEvent, Contact, ContactGroup, NotifyRoute


def route_matches(match: dict, alarm: AlarmEvent) -> bool:
    """match 中各条件为空表示不限；tenant_ids 用于“租户B 的告警发给租户B 的运维组”。"""
    return all([
        not match.get("tenant_ids") or alarm.tenant_id in match["tenant_ids"],
        not match.get("services") or alarm.service in match["services"],
        not match.get("severities") or alarm.severity in match["severities"],
        not match.get("resource_types") or alarm.resource_type in match["resource_types"],
    ])


def resolve_recipients(db: Session, alarm: AlarmEvent) -> tuple[list[str], list[str]]:
    """按优先级匹配通知路由，返回（收件人邮箱, 命中的路由名）。continue_match=True 时继续匹配后续路由。"""
    emails: list[str] = []
    names: list[str] = []
    routes = db.scalars(select(NotifyRoute).where(NotifyRoute.enabled.is_(True)).order_by(NotifyRoute.priority))
    for r in routes:
        if not route_matches(r.match or {}, alarm):
            continue
        group = db.scalar(select(ContactGroup).where(ContactGroup.code == r.group_code))
        if group and group.member_ids:
            rows = db.scalars(select(Contact.email).where(Contact.id.in_(group.member_ids), Contact.enabled.is_(True)))
            emails.extend(e for e in rows if e not in emails)
        names.append(r.name)
        if not r.continue_match:
            break
    return emails, names
