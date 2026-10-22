from datetime import datetime

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JsonType, UtcDateTime

from .base import utcnow


class Silence(Base):
    """维护窗口与告警静默。
    - kind：SILENCE（临时静默，例如在告警详情里“静默 2 小时”）/ MAINTENANCE（维护窗口，变更、发布、演练）
    - matchers：{tenant_ids, services, resource_types, rule_ids, severities, resource_refs, fingerprints, name_contains}，
      各条件为空表示不限，条件之间为“且”；至少要有一个条件（防止误把全部告警静默）
    - recurrence：为空 = 一次性（starts_at ~ ends_at 内一直生效）；
      {"weekdays": [4], "start": "22:00", "end": "02:00"} = 在有效期内每周四 22:00 至次日 02:00 生效（平台时区）
    - 命中静默的告警照常记录，状态为 SUPPRESSED、不发通知；静默结束后仍未恢复的告警转回 FIRING 并补发通知
    """

    __tablename__ = "alarm_silence"
    __table_args__ = (Index("idx_silence_window", "starts_at", "ends_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16), default="SILENCE")
    matchers: Mapped[dict] = mapped_column(JsonType, default=dict)
    recurrence: Mapped[dict] = mapped_column(JsonType, default=dict)
    starts_at: Mapped[datetime] = mapped_column(UtcDateTime())
    ends_at: Mapped[datetime] = mapped_column(UtcDateTime())
    reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow, onupdate=utcnow)
    cancelled_by: Mapped[str | None] = mapped_column(String(64))
    cancelled_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    suppressed_total: Mapped[int] = mapped_column(Integer, default=0)      # 累计静默的告警条数（列表展示）
