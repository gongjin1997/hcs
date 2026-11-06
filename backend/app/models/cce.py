"""CCE 管理面最新快照；各数据源独立保存成功时间和本轮结果。"""
from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JsonType, UtcDateTime


class CceClusterSnapshot(Base):
    __tablename__ = "cce_cluster_snapshot"
    __table_args__ = (Index("idx_cce_snapshot_scope", "tenant_id", "project_ref", "cluster_id"),)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer)
    project_ref: Mapped[int] = mapped_column(Integer)
    cluster_id: Mapped[str] = mapped_column(String(255))
    cluster: Mapped[dict] = mapped_column(JsonType, default=dict)
    nodes: Mapped[list] = mapped_column(JsonType, default=list)
    nodepools: Mapped[list] = mapped_column(JsonType, default=list)
    native: Mapped[dict] = mapped_column(JsonType, default=dict)
    sources: Mapped[dict] = mapped_column(JsonType, default=dict)
    last_seen: Mapped[datetime] = mapped_column(UtcDateTime())
    missing: Mapped[bool] = mapped_column(Boolean, default=False)
