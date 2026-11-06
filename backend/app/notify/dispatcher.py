"""通知 Outbox 消费者：取出 PENDING / 到期 RETRYING 的通知，渲染并发送；失败按退避重试。"""
import logging
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from email.message import EmailMessage
from time import perf_counter
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.logging import log_context
from app.models import AlarmEvent, AlarmEventLog, Notification, User
from app.models.base import utcnow
from app.services import mail_settings_service, silence_service

from .render import render_alarm_mail
from .smtp import build_message, safe_error, send

BACKOFF = [60, 300, 900, 1800, 3600]   # 秒；第 5 次仍失败则置为 FAILED 并触发平台自监控告警
log = logging.getLogger(__name__)


def _obsolete_reason(n: Notification, alarm: AlarmEvent, reopened_at: datetime | None, silences: list) -> str | None:
    """发送前复核轮次和静默；SMTP 受理不能绕过刚生效的维护窗口。"""
    if n.kind not in ("FIRING", "UPGRADE", "ESCALATE", "REPEAT", "RESOLVED"):
        return None
    if reopened_at is not None and n.created_at < reopened_at:
        return "所属告警轮次已结束"
    if n.kind == "RESOLVED":
        return None if alarm.status in ("RESOLVED", "CLOSED") else "告警当前未恢复"
    if n.kind in ("FIRING", "UPGRADE"):
        if alarm.status == "SUPPRESSED" or any(silence_service.matches(s.matchers or {}, alarm) for s in silences):
            return "告警当前处于静默或维护窗口"
        return None
    if alarm.status != "FIRING":
        return "告警已认领、静默、恢复或关闭"
    if any(silence_service.matches(s.matchers or {}, alarm) for s in silences):
        return "告警当前处于静默或维护窗口"
    return None


def _record(db: Session, alarm: AlarmEvent, n: Notification, action: str, note: str, now: datetime) -> None:
    """在现有时间线追加结果与实际邮箱快照，后续人员/规则修改不重写历史。"""
    recipients = "、".join(n.recipients or []) or "无"
    db.add(AlarmEventLog(alarm_id=alarm.id, action=action, operator="system", at=now,
        note=f"通知 #{n.id}（{n.kind}）；收件人：{recipients}；{note}"))


def dispatch_pending(db: Session, sender: Callable[[EmailMessage], None] = send, now: datetime | None = None,
                     batch: int = 50) -> int:
    with log_context(notification_batch_id=uuid4().hex):
        return _dispatch_pending(db, sender, now, batch)


def _dispatch_pending(db: Session, sender: Callable[[EmailMessage], None], now: datetime | None, batch: int) -> int:
    started = perf_counter()
    now = now or utcnow()
    q = (select(Notification)
         .where(Notification.status.in_(("PENDING", "RETRYING")),
                or_(Notification.next_retry_at.is_(None), Notification.next_retry_at <= now))
         .order_by(Notification.id).limit(batch))
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True)     # 多个 notifier 实例并行消费
    sent = 0
    failed: list[Notification] = []
    managed = list(db.scalars(select(User).where(User.email.is_not(None))))
    users_by_email: dict[str, list[User]] = {}
    for user in managed:
        if not user.email:
            continue
        users_by_email.setdefault(user.email.casefold(), []).append(user)
    pending = list(db.scalars(q).all())
    if pending:
        log.info("notification_batch_started", extra={"event": "notification_batch_started", "pending_count": len(pending)})
    guarded_ids = {n.alarm_id for n in pending
                   if n.alarm_id and n.kind in ("FIRING", "UPGRADE", "ESCALATE", "REPEAT", "RESOLVED")}
    reopened = dict(db.execute(select(AlarmEventLog.alarm_id, func.max(AlarmEventLog.at)).where(
        AlarmEventLog.alarm_id.in_(guarded_ids), AlarmEventLog.action == "REOPEN").group_by(AlarmEventLog.alarm_id)).all())
    silences = silence_service.active_silences(db, now) if any(
        n.kind in ("FIRING", "UPGRADE", "ESCALATE", "REPEAT") for n in pending) else []
    for n in pending:
        alarm = db.get(AlarmEvent, n.alarm_id) if n.alarm_id else None
        if alarm is None:
            n.status, n.error, n.next_retry_at = "FAILED", "告警不存在", None
            log.warning("notification_rejected", extra={"event": "notification_rejected", "notification_id": n.id,
                                                        "alarm_id": n.alarm_id, "reason": "alarm_missing"})
            continue
        reason = _obsolete_reason(n, alarm, reopened.get(alarm.id), silences)
        if reason:
            # 沿用已有 MERGED 非投递终态，保留记录及原因，不制造 SMTP 失败告警。
            n.status, n.error, n.next_retry_at = "MERGED", f"过时通知已跳过：{reason}", None
            _record(db, alarm, n, "NOTIFY_SKIP", f"未发送：{reason}", now)
            continue
        recipients = list(dict.fromkeys(n.recipients or []))
        if not recipients:
            n.status, n.error, n.next_retry_at = "FAILED", "没有收件人", None
            _record(db, alarm, n, "NOTIFY_FAIL", f"未发送：{n.error}", now)
            log.warning("notification_rejected", extra={"event": "notification_rejected", "notification_id": n.id,
                                                        "alarm_id": alarm.id, "reason": "no_recipient"})
            continue
        if len(recipients) > 1:
            # 每个收件人有自己的签名链接和重试记录，不重发已获 SMTP 受理的通知。
            n.recipients = [recipients[0]]
            copies = [Notification(alarm_id=n.alarm_id, kind=n.kind, channel=n.channel, recipients=[recipient],
                                   subject=n.subject, status=n.status, retry=n.retry, next_retry_at=n.next_retry_at,
                                   created_at=n.created_at) for recipient in recipients[1:]]
            db.add_all(copies)
            db.flush()
            pending.extend(copies)
        recipient = n.recipients[0].casefold()
        known_users = users_by_email.get(recipient, [])
        if not known_users or not any(
            user.enabled and user.notify_enabled and (user.tenant_ids is None or alarm.tenant_id in user.tenant_ids)
            for user in known_users
        ):
            n.status, n.error, n.next_retry_at = "FAILED", "用户未登记、已停用通知或不属于当前租户，未发送", None
            _record(db, alarm, n, "NOTIFY_FAIL", n.error, now)
            log.warning("notification_rejected", extra={"event": "notification_rejected", "notification_id": n.id,
                                                        "alarm_id": alarm.id, "reason": "contact_disabled"})
            continue
        with log_context(notification_id=n.id, alarm_id=alarm.id, tenant_id=alarm.tenant_id,
                         notification_kind=n.kind):
            try:
                subject, html, text = render_alarm_mail(db, alarm, n.kind, n.recipients[0] if n.recipients else "")
                root = f"<alarm-{alarm.id}@hcs-inspector>"
                first = n.kind == "FIRING" and alarm.reopen_count == 0 and not db.scalar(
                    select(Notification.id).where(Notification.alarm_id == alarm.id, Notification.status == "SENT"))
                mid = root if first else f"<alarm-{alarm.id}-n{n.id}@hcs-inspector>"
                sender(build_message(subject=subject, html=html, text=text, recipients=n.recipients,
                                     message_id=mid, thread_root=root, severity=alarm.severity, alarm_id=alarm.id,
                                     fingerprint=alarm.fingerprint, settings=mail_settings_service.get(db).smtp))
                n.subject = subject
                n.status, n.sent_at, n.message_id, n.error, n.next_retry_at = "SENT", now, mid, None, None
                _record(db, alarm, n, "NOTIFY",
                    f"第 {n.retry + 1} 次尝试已提交 SMTP（不代表收件人送达）；主题：{subject}；"
                    f"资源：{alarm.resource_name} / {alarm.sub_object or '-'}；"
                    f"当前值：{alarm.current_value or '未知'}；阈值：{alarm.threshold_text or '未知'}；Message-ID：{mid}", now)
                sent += 1
                log.debug("notification_smtp_accepted", extra={"event": "notification_smtp_accepted", "status": "SENT"})
            except Exception as e:  # noqa: BLE001  任何发送异常都进入重试
                n.retry += 1
                n.error = safe_error(e)
                if n.retry >= len(BACKOFF):
                    n.status, n.next_retry_at = "FAILED", None
                    failed.append(n)
                else:
                    n.status, n.next_retry_at = "RETRYING", now + timedelta(seconds=BACKOFF[n.retry - 1])
                next_attempt = f"；下次重试：{n.next_retry_at.isoformat()}" if n.next_retry_at else "；不再自动重试"
                _record(db, alarm, n, "NOTIFY_FAIL" if n.status == "FAILED" else "NOTIFY_RETRY",
                    f"第 {n.retry} 次尝试失败：{n.error}{next_attempt}", now)
                log.log(logging.ERROR if n.status == "FAILED" else logging.WARNING, "notification_delivery_failed",
                        extra={"event": "notification_delivery_failed", "status": n.status, "retry": n.retry,
                               "next_retry_at": n.next_retry_at, "exception_type": type(e).__name__},
                        exc_info=(RuntimeError, RuntimeError(type(e).__name__), e.__traceback__))
        db.flush()  # 后续单收件人邮件的主题线程判断必须能看到已成功发送的记录。
    _platform_hooks(db, sent, failed)
    outcome = {"event": "notification_batch_finished", "notification_count": len(pending), "sent_count": sent,
               "statuses": dict(Counter(n.status for n in pending)), "notification_ids": [n.id for n in pending]}
    db.commit()
    if pending:
        log.info("notification_batch_finished", extra={**outcome, "cost_ms": round((perf_counter() - started) * 1000, 2)})
    return sent


def _platform_hooks(db: Session, sent: int, failed: list[Notification]) -> None:
    """平台自身告警 R-PLAT-003：通知最终发送失败时告警，之后有通知发送成功时自动恢复。异常不影响发送本身。"""
    from app.services import platform_watch

    try:
        for n in failed:
            platform_watch.on_notification_failed(db, n)
        if sent:
            platform_watch.on_notification_sent(db)
    except Exception as exc:  # noqa: BLE001
        log.warning("notification_platform_hook_failed", extra={"event": "notification_platform_hook_failed",
                    "exception_type": type(exc).__name__},
                    exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
