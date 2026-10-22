from datetime import datetime

from sqlalchemy import Boolean, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JsonType, UtcDateTime

from .base import utcnow


class Resource(Base):
    """资源台账（CMDB）：云资源 + 巡检对象。resource_type 取值见 app.services.catalog.RESOURCE_TYPES。

    - 台账字段（region / az / ip / spec / vcpus ...）由“资源台账同步”任务从 HCS 拉取
    - 管理字段（owner / business / env_type / remark / critical）由运维在页面维护，同步不会覆盖
    - 健康字段（health / metrics）由巡检流水线回写，只有巡检对象才有
    """

    __tablename__ = "resource"
    __table_args__ = (
        UniqueConstraint("resource_type", "resource_id"),
        Index("idx_resource_type_health", "resource_type", "health"),
        Index("idx_resource_lifecycle", "lifecycle", "resource_type"),
        Index("idx_resource_tenant_type", "tenant_id", "resource_type"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer)               # 所属租户（hcs_account.id）
    project_ref: Mapped[int | None] = mapped_column(Integer)             # 所属项目（hcs_project.id），台账同步按项目比对
    service: Mapped[str] = mapped_column(String(32))               # ecs / evs / dcs / mrs ...
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    status_raw: Mapped[str | None] = mapped_column(String(64))
    health: Mapped[str] = mapped_column(String(16), default="UNKNOWN")  # OK/INFO/WARNING/MAJOR/CRITICAL/UNKNOWN
    # ---- 台账字段（同步写入）----
    region: Mapped[str | None] = mapped_column(String(64))
    az: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(128))
    spec: Mapped[str | None] = mapped_column(String(128))              # 规格摘要，例如 “8 vCPU | 32 GB”
    vcpus: Mapped[int | None] = mapped_column(Integer)
    memory_gb: Mapped[float | None] = mapped_column(Float)
    storage_gb: Mapped[float | None] = mapped_column(Float)
    created_time: Mapped[datetime | None] = mapped_column(UtcDateTime())   # 云上创建时间
    labels: Mapped[dict] = mapped_column(JsonType, default=dict)
    attrs: Mapped[dict] = mapped_column(JsonType, default=dict)         # 类型特有属性（操作系统、网段、带宽...）
    metrics: Mapped[dict] = mapped_column(JsonType, default=dict)       # 最新关键指标，供列表展示
    expected_state: Mapped[dict] = mapped_column(JsonType, default=dict)
    # ---- 管理字段（页面维护）----
    owner: Mapped[str | None] = mapped_column(String(64))
    business: Mapped[str | None] = mapped_column(String(64))            # 所属业务系统
    env_type: Mapped[str | None] = mapped_column(String(16))            # PROD / TEST / DEV
    remark: Mapped[str | None] = mapped_column(Text)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    critical: Mapped[bool] = mapped_column(Boolean, default=False)
    # ---- 生命周期 ----
    lifecycle: Mapped[str] = mapped_column(String(16), default="ACTIVE")   # ACTIVE / DELETED（云上已释放）
    first_seen: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    status_since: Mapped[datetime | None] = mapped_column(UtcDateTime())


class ResourceChange(Base):
    """台账变更记录：同步发现的新增 / 变更 / 释放、巡检发现的状态变更与重启，以及页面上的人工修改。"""

    __tablename__ = "resource_change"
    __table_args__ = (Index("idx_change_time", "detected_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, index=True)
    resource_ref: Mapped[int] = mapped_column(Integer, index=True)
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    change_type: Mapped[str] = mapped_column(String(16))      # CREATED / UPDATED / DELETED / STATUS（状态变更）/ REBOOT（重启）
    changes: Mapped[list] = mapped_column(JsonType, default=list)   # [{"field", "label", "old", "new"}]
    source: Mapped[str] = mapped_column(String(16), default="SYNC")  # SYNC（台账同步）/ MANUAL（页面）/ INSPECT（巡检）
    operator: Mapped[str] = mapped_column(String(64), default="system")
    detected_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
