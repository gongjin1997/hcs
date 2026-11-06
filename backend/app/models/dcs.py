"""DCS 实例的实际内存采集点；不从资源快照回填或预造历史。"""
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UtcDateTime


class DcsMemorySample(Base):
    """一资源一本地记录时刻一条；新记录用响应接收时间，并非源端采样时刻。"""

    __tablename__ = "dcs_memory_sample"
    __table_args__ = (
        Index("uk_dcs_memory_resource_time", "resource_ref", "observed_at", unique=True),
        Index("idx_dcs_memory_observed_at", "observed_at"),
        CheckConstraint("memory_usage >= 0", name="ck_dcs_memory_usage"),
        CheckConstraint("used_memory_mb IS NULL OR used_memory_mb >= 0", name="ck_dcs_memory_used"),
        CheckConstraint("max_memory_mb IS NULL OR max_memory_mb > 0", name="ck_dcs_memory_max"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_ref: Mapped[int] = mapped_column(ForeignKey("resource.id", ondelete="CASCADE"))
    tenant_id: Mapped[int | None] = mapped_column(Integer)
    project_ref: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime())
    memory_usage: Mapped[float] = mapped_column(Float)
    used_memory_mb: Mapped[float | None] = mapped_column(Float)
    max_memory_mb: Mapped[float | None] = mapped_column(Float)


class DcsMetricSample(Base):
    """V2 原生统计的同次响应；不回填旧快照，单位与版本不可混合。"""

    __tablename__ = "dcs_metric_sample"
    __table_args__ = (
        Index("uk_dcs_metric_resource_time", "resource_ref", "observed_at", unique=True),
        Index("idx_dcs_metric_scope_time", "tenant_id", "project_ref", "observed_at"),
        Index("idx_dcs_metric_observed_at", "observed_at"),
        CheckConstraint("api_version = 'v2' AND source = 'DCS'", name="ck_dcs_metric_source"),
        *(CheckConstraint(f"{key} IS NULL OR {key} >= 0", name=f"ck_dcs_metric_{key}") for key in (
            "memory_usage", "used_memory_mb", "used_cpu_seconds", "input_kbps", "output_kbps",
            "keys", "cmd_get_count", "cmd_set_count")),
        CheckConstraint("max_memory_mb IS NULL OR max_memory_mb > 0", name="ck_dcs_metric_max_memory_mb"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_ref: Mapped[int] = mapped_column(ForeignKey("resource.id", ondelete="CASCADE"))
    tenant_id: Mapped[int | None] = mapped_column(Integer)
    project_ref: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime())
    collected_at: Mapped[datetime] = mapped_column(UtcDateTime())
    source: Mapped[str] = mapped_column(String(16))
    api_version: Mapped[str] = mapped_column(String(8))
    endpoint: Mapped[str] = mapped_column(String(32))
    memory_usage: Mapped[float | None] = mapped_column(Float)
    used_memory_mb: Mapped[float | None] = mapped_column(Float)
    max_memory_mb: Mapped[float | None] = mapped_column(Float)
    used_cpu_seconds: Mapped[float | None] = mapped_column(Float)
    input_kbps: Mapped[float | None] = mapped_column(Float)
    output_kbps: Mapped[float | None] = mapped_column(Float)
    keys: Mapped[int | None] = mapped_column(BigInteger)
    cmd_get_count: Mapped[int | None] = mapped_column(BigInteger)
    cmd_set_count: Mapped[int | None] = mapped_column(BigInteger)
