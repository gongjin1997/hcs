"""通知 Outbox 消费者：取出 PENDING / 到期 RETRYING 的通知，渲染并发送；失败按退避重试。"""
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from email.message import EmailMessage

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import AlarmEvent, AlarmEventLog, Notification
from app.models.base import utcnow

from .render import render_alarm_mail
from .smtp import build_message, send

BACKOFF = [60, 300, 900, 1800, 3600]   # 秒；第 5 次仍失败则置为 FAILED 并触发平台自监控告警
log = logging.getLogger(__name__)


def dispatch_pending(db: Session, sender: Callable[[EmailMessage], None] = send, now: datetime | None = None,
                     batch: int = 50) -> int:
    now = now or utcnow()
    q = (select(Notification)
         .where(Notification.status.in_(("PENDING", "RETRYING")),
                or_(Notification.next_retry_at.is_(None), Notification.next_retry_at <= now))
         .order_by(Notification.id).limit(batch))
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True)     # 多个 notifier 实例并行消费
    sent = 0
    failed: list[Notification] = []
    for n in db.scalars(q).all():
        alarm = db.get(AlarmEvent, n.alarm_id) if n.alarm_id else None
        if alarm is None:
            n.status, n.error = "FAILED", "告警不存在"
            continue
        try:
            subject, html, text = render_alarm_mail(db, alarm, n.kind, n.recipients[0] if n.recipients else "")
            root = f"<alarm-{alarm.id}@hcs-inspector>"
            first = n.kind == "FIRING" and alarm.reopen_count == 0 and not db.scalar(
                select(Notification.id).where(Notification.alarm_id == alarm.id, Notification.status == "SENT"))
            mid = root if first else f"<alarm-{alarm.id}-n{n.id}@hcs-inspector>"
            sender(build_message(subject=n.subject or subject, html=html, text=text, recipients=n.recipients,
                                 message_id=mid, thread_root=root, severity=alarm.severity, alarm_id=alarm.id,
                                 fingerprint=alarm.fingerprint))
            n.status, n.sent_at, n.message_id, n.error = "SENT", now, mid, None
            db.add(AlarmEventLog(alarm_id=alarm.id, action="NOTIFY", operator="system", at=now,
                                 note=f"{n.kind} 邮件已发送：{len(n.recipients)} 位收件人"))
            sent += 1
        except Exception as e:  # noqa: BLE001  任何发送异常都进入重试
            n.retry += 1
            n.error = f"{type(e).__name__}: {e}"[:500]
            if n.retry >= len(BACKOFF):
                n.status = "FAILED"
                failed.append(n)
                log.error("notification %s failed permanently: %s", n.id, n.error)
            else:
                n.status, n.next_retry_at = "RETRYING", now + timedelta(seconds=BACKOFF[n.retry - 1])
    _platform_hooks(db, sent, failed)
    db.commit()
    return sent


def _platform_hooks(db: Session, sent: int, failed: list[Notification]) -> None:
    """平台自身告警 R-PLAT-003：通知最终发送失败时告警，之后有通知发送成功时自动恢复。异常不影响发送本身。"""
    from app.services import platform_watch

    try:
        for n in failed:
            platform_watch.on_notification_failed(db, n)
        if sent:
            platform_watch.on_notification_sent(db)
    except Exception:  # noqa: BLE001
        log.exception("platform watch hook failed")
