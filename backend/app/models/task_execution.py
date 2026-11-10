"""持久巡检队列；一个执行对应一个任务、租户和项目。"""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JsonType, UtcDateTime

from .base import utcnow


class TaskExecution(Base):
    __tablename__ = "task_execution"
    __table_args__ = (
        UniqueConstraint("request_key", "scope_key", name="uq_task_execution_request_scope"),
        Index("ix_task_execution_queue", "status", "created_at"),
        Index("ix_task_execution_scope_time", "tenant_id", "project_ref", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("inspection_run.id", ondelete="CASCADE"), unique=True)
    task_code: Mapped[str] = mapped_column(String(64), index=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer)
    project_ref: Mapped[int | None] = mapped_column(Integer)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    request_key: Mapped[str] = mapped_column(String(128))
    scope_key: Mapped[str] = mapped_column(String(64))
    active_scope: Mapped[str | None] = mapped_column(String(64), unique=True)
    trigger_type: Mapped[str] = mapped_column(String(16), default="MANUAL")
    requested_by: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="QUEUED")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    heartbeat_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    deadline_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    queue_expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    timeout_sec: Mapped[int] = mapped_column(Integer, default=300)
    owner_token: Mapped[str | None] = mapped_column(String(64))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    params: Mapped[dict] = mapped_column(JsonType, default=dict)
    target: Mapped[dict] = mapped_column(JsonType, default=dict)
    progress: Mapped[dict] = mapped_column(JsonType, default=dict)
    history: Mapped[list] = mapped_column(JsonType, default=list)
    error_code: Mapped[str | None] = mapped_column(String(64))


class TaskExecutionCancel(Base):
    __tablename__ = "task_execution_cancel"
    execution_id: Mapped[str] = mapped_column(ForeignKey("task_execution.id", ondelete="CASCADE"), primary_key=True)
    requested_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    requested_by: Mapped[str] = mapped_column(String(128))
