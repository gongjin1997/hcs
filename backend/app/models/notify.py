from datetime import datetime

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JsonType, UtcDateTime

from .base import utcnow


class Contact(Base):
    __tablename__ = "contact"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    email: Mapped[str] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ContactGroup(Base):
    __tablename__ = "contact_group"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    member_ids: Mapped[list] = mapped_column(JsonType, default=list)
    leader_ids: Mapped[list] = mapped_column(JsonType, default=list)


class NotifyRoute(Base):
    __tablename__ = "notify_route"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    priority: Mapped[int] = mapped_column(Integer, default=100)
    match: Mapped[dict] = mapped_column(JsonType, default=dict)     # {"service": [...], "severities": [...]}
    group_code: Mapped[str] = mapped_column(String(64))
    escalation: Mapped[list] = mapped_column(JsonType, default=list)
    continue_match: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Notification(Base):
    """通知 Outbox：与告警状态变更同事务写入，由 notifier 进程异步发送。"""

    __tablename__ = "notification"
    id: Mapped[int] = mapped_column(primary_key=True)
    alarm_id: Mapped[int | None] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16))        # FIRING/UPGRADE/REPEAT/ESCALATE/RESOLVED/DIGEST/REPORT
    channel: Mapped[str] = mapped_column(String(16), default="SMTP")
    recipients: Mapped[list] = mapped_column(JsonType, default=list)
    subject: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING/SENT/RETRYING/FAILED/MERGED
    retry: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    message_id: Mapped[str | None] = mapped_column(String(255))
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
