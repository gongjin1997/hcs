"""短信升级 Outbox：每条记录仅引用一位统一用户，不保存完整手机号或网关凭据。"""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UtcDateTime

from .base import utcnow


class SmsNotification(Base):
    __tablename__ = "sms_notification"
    __table_args__ = (
        Index("uk_sms_notification_dedup", "dedup_key", unique=True),
        Index("idx_sms_notification_status", "status", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("hcs_account.id", ondelete="CASCADE"), index=True)
    alarm_id: Mapped[int] = mapped_column(ForeignKey("alarm_event.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("sys_user.id", ondelete="SET NULL"), index=True)
    alarm_generation: Mapped[int] = mapped_column(Integer)
    dedup_key: Mapped[str] = mapped_column(String(64))
    config_revision: Mapped[int] = mapped_column(Integer)
    route_id: Mapped[int] = mapped_column(Integer)
    after_min: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    content: Mapped[str] = mapped_column(Text)
    phone_mask: Mapped[str | None] = mapped_column(String(32))
    gateway_code: Mapped[str | None] = mapped_column(String(32))
    gateway_message_id: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
