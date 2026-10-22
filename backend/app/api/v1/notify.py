from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import Scope, get_scope, require_perm
from app.core.response import ok
from app.models import AlarmEvent, Contact, ContactGroup, Notification, NotifyRoute
from app.notify.render import render_alarm_mail
from app.notify.routing import resolve_recipients
from app.schemas.alarm import NotificationOut
from app.services.alarm_service import get_alarm
from app.services.tenancy import tenant_map

router = APIRouter(prefix="/notify", tags=["通知"], dependencies=[Depends(require_perm("notify:view"))])


@router.get("/routes", summary="通知路由")
def routes(db: Session = Depends(get_db)):
    tm = tenant_map(db)
    return ok([{"id": r.id, "name": r.name, "priority": r.priority, "match": r.match, "group_code": r.group_code,
                "tenant_names": [tm[t].short_name or tm[t].name for t in (r.match or {}).get("tenant_ids") or []
                                 if t in tm],
                "escalation": r.escalation, "continue_match": r.continue_match, "enabled": r.enabled}
               for r in db.scalars(select(NotifyRoute).order_by(NotifyRoute.priority))])


@router.get("/groups", summary="联系组（含成员）")
def groups(db: Session = Depends(get_db)):
    contacts = {c.id: c for c in db.scalars(select(Contact))}
    pick = lambda ids: [{"id": i, "name": contacts[i].name, "email": contacts[i].email} for i in ids if i in contacts]  # noqa: E731
    return ok([{"id": g.id, "code": g.code, "name": g.name, "members": pick(g.member_ids),
                "leaders": pick(g.leader_ids)} for g in db.scalars(select(ContactGroup).order_by(ContactGroup.id))])


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
