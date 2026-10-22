from datetime import datetime

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JsonType, UtcDateTime

from .base import utcnow


class Report(Base):
    """报表快照：日报 / 周报 / 月报 / 台账报表。生成后内容固定，便于归档和对比。"""

    __tablename__ = "report"
    __table_args__ = (Index("idx_report_kind_period", "kind", "period_start"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))              # DAILY / WEEKLY / MONTHLY / LEDGER
    tenant_id: Mapped[int | None] = mapped_column(Integer)     # 为空 = 全部租户合并统计
    title: Mapped[str] = mapped_column(String(128))
    period_start: Mapped[datetime] = mapped_column(UtcDateTime())
    period_end: Mapped[datetime] = mapped_column(UtcDateTime())
    summary: Mapped[dict] = mapped_column(JsonType, default=dict)   # 列表页展示的关键数字
    data: Mapped[dict] = mapped_column(JsonType, default=dict)      # 报表完整内容
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    sent_to: Mapped[list] = mapped_column(JsonType, default=list)
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    send_error: Mapped[str | None] = mapped_column(Text)
