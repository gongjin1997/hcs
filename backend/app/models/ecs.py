"""ECS 真实指标采样；观测时间来自上游，不从台账快照回填。"""
from datetime import datetime

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JsonType, UtcDateTime


class EcsMetricSample(Base):
    __tablename__ = "ecs_metric_sample"
    __table_args__ = (
        Index("uk_ecs_metric_sample", "resource_ref", "metric", "source", "observed_at", unique=True),
        Index("idx_ecs_metric_scope_time", "tenant_id", "project_ref", "observed_at"),
        Index("idx_ecs_metric_observed_at", "observed_at"),
        CheckConstraint("value >= 0 AND value <= 100", name="ck_ecs_metric_percent"),
        CheckConstraint("metric IN ('cpu_util', 'mem_util', 'disk_util')", name="ck_ecs_metric_name"),
        CheckConstraint("period_sec >= 0", name="ck_ecs_metric_period"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_ref: Mapped[int] = mapped_column(ForeignKey("resource.id", ondelete="CASCADE"))
    tenant_id: Mapped[int | None] = mapped_column(Integer)
    project_ref: Mapped[int | None] = mapped_column(Integer)
    metric: Mapped[str] = mapped_column(String(32))
    value: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16))
    unit: Mapped[str] = mapped_column(String(16), default="%")
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime())
    collected_at: Mapped[datetime] = mapped_column(UtcDateTime())
    period_sec: Mapped[int] = mapped_column(Integer, default=0)
    dimensions: Mapped[dict] = mapped_column(JsonType, default=dict)
