from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JsonType, UtcDateTime

from .base import utcnow


class InspectionTask(Base):
    __tablename__ = "inspection_task"
    code: Mapped[str] = mapped_column(String(64), primary_key=True)    # 例如 dcs_status
    name: Mapped[str] = mapped_column(String(64))
    service: Mapped[str] = mapped_column(String(32))
    interval_sec: Mapped[int] = mapped_column(Integer)
    cron: Mapped[str | None] = mapped_column(String(64))              # 设置后按 crontab 执行（优先于 interval_sec）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    params: Mapped[dict] = mapped_column(JsonType, default=dict)


class InspectionRun(Base):
    __tablename__ = "inspection_run"
    __table_args__ = (Index("idx_inspection_run_scope_time", "task_code", "tenant_id", "project_ref", "started_at", "id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    task_code: Mapped[str] = mapped_column(String(64), index=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer)                       # 每个租户单独一条运行记录
    project_ref: Mapped[int | None] = mapped_column(Integer)  # NULL = 历史未知项目；全局内部任务不适用，不回填推断
    trigger_type: Mapped[str] = mapped_column(String(16), default="SCHEDULE")   # SCHEDULE / MANUAL
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")  # RUNNING/SUCCESS/PARTIAL/FAILED/TIMEOUT/SKIPPED
    resource_total: Mapped[int] = mapped_column(Integer, default=0)
    abnormal_total: Mapped[int] = mapped_column(Integer, default=0)
    api_calls: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict] = mapped_column(JsonType, default=dict)      # 台账同步：按资源类型的新增 / 变更 / 释放数
