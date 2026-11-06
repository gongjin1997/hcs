"""GaussDB 原生存储定时采样；失败保留空值，禁止配置/快照回填。"""
from datetime import datetime

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UtcDateTime


class GaussDBStorageSample(Base):
    __tablename__ = "gaussdb_storage_sample"
    __table_args__ = (
        Index("uk_gaussdb_storage_resource_time", "resource_ref", "observed_at", unique=True),
        Index("idx_gaussdb_storage_scope_time", "tenant_id", "project_ref", "resource_ref", "observed_at"),
        Index("idx_gaussdb_storage_observed_at", "observed_at"),
        CheckConstraint("used_gb IS NULL OR used_gb >= 0", name="ck_gaussdb_storage_used"),
        CheckConstraint("total_gb IS NULL OR total_gb >= 0", name="ck_gaussdb_storage_total"),
        CheckConstraint("usage_percent IS NULL OR usage_percent >= 0", name="ck_gaussdb_storage_usage"),
        CheckConstraint("status IN ('AVAILABLE', 'PARTIAL', 'ERROR', 'UNAVAILABLE')", name="ck_gaussdb_storage_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_ref: Mapped[int] = mapped_column(ForeignKey("resource.id", ondelete="CASCADE"))
    tenant_id: Mapped[int] = mapped_column(Integer)
    project_ref: Mapped[int] = mapped_column(Integer)
    # volume-usage 不返回源端时间；两列均记录本地响应接收/失败尝试时间，observed_at 兼容既有查询索引。
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime())
    collected_at: Mapped[datetime] = mapped_column(UtcDateTime())
    status: Mapped[str] = mapped_column(String(16))
    used_gb: Mapped[float | None] = mapped_column(Float)
    total_gb: Mapped[float | None] = mapped_column(Float)
    usage_percent: Mapped[float | None] = mapped_column(Float)
