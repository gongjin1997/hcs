from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AlarmEvent, Contact, ContactGroup, NotifyRoute, User
from app.services.identity import contact_users


def route_matches(match: dict, alarm: AlarmEvent) -> bool:
    """match 中各条件为空表示不限；tenant_ids 用于“租户B 的告警发给租户B 的运维组”。"""
    return all([
        not match.get("tenant_ids") or alarm.tenant_id in match["tenant_ids"],
        not match.get("services") or alarm.service in match["services"],
        not match.get("severities") or alarm.severity in match["severities"],
        not match.get("resource_types") or alarm.resource_type in match["resource_types"],
    ])


@dataclass
class RoutingSnapshot:
    routes: list[NotifyRoute]
    groups: dict[str, ContactGroup]
    emails: dict[int, str]
    users: dict[int, User] = field(default_factory=dict)


def contact_allows(user: User | None, tenant_id: int | None) -> bool:
    return user is not None and (user.enabled and user.notify_enabled and bool(user.email)
                            and (user.tenant_ids is None or tenant_id in user.tenant_ids))


def load_routing(db: Session) -> RoutingSnapshot:
    users = contact_users(db)
    emails: dict[int, str] = {}
    for c in db.scalars(select(Contact)):
        user = users.get(c.id)
        if user is not None:
            if user.enabled and user.notify_enabled and user.email:
                emails[c.id] = user.email
    return RoutingSnapshot(
        list(db.scalars(select(NotifyRoute).where(NotifyRoute.enabled.is_(True))
                        .order_by(NotifyRoute.priority, NotifyRoute.id))),
        {g.code: g for g in db.scalars(select(ContactGroup))}, emails, users)


def resolve_recipients(db: Session, alarm: AlarmEvent, snapshot: RoutingSnapshot | None = None) -> tuple[list[str], list[str]]:
    """按优先级匹配通知路由，返回（收件人邮箱, 命中的路由名）。continue_match=True 时继续匹配后续路由。"""
    emails: list[str] = []
    names: list[str] = []
    snapshot = snapshot or load_routing(db)
    for r in snapshot.routes:
        if not route_matches(r.match or {}, alarm):
            continue
        group = snapshot.groups.get(r.group_code)
        if group and group.member_ids:
            rows = [snapshot.emails[cid] for cid in group.member_ids
                    if cid in snapshot.emails and contact_allows(snapshot.users.get(cid), alarm.tenant_id)]
            emails.extend(e for e in rows if e not in emails)
        names.append(r.name)
        if not r.continue_match:
            break
    return emails, names
