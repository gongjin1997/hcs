from datetime import datetime

from sqlalchemy import Boolean, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JsonType, UtcDateTime

from .base import utcnow


class SysSetting(Base):
    """页面可修改的系统设置（键值，值为 JSON）：数据保留期、定时报表、平台自身告警、台账字典等。
    环境变量里的同名配置只作为默认值；页面保存后以数据库为准（见 app.services.settings_service）。"""

    __tablename__ = "sys_setting"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JsonType, default=dict)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow, onupdate=utcnow)


class CollectSwitch(Base):
    """采集开关：按资源类型启停台账同步与巡检。tenant_id = 0 为全局默认，其他为租户单独设置（覆盖全局）。
    没有记录 = 开启（默认全部采集）。"""

    __tablename__ = "collect_switch"
    __table_args__ = (UniqueConstraint("tenant_id", "resource_type"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=0)
    resource_type: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow, onupdate=utcnow)


class WorkerHeartbeat(Base):
    """后台进程心跳（scheduler / notifier 每 30 秒写一次）：平台状态页、/metrics 和“进程心跳超时”平台告警使用。"""

    __tablename__ = "worker_heartbeat"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    host: Mapped[str | None] = mapped_column(String(128))
    pid: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[str | None] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    beat_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    info: Mapped[dict] = mapped_column(JsonType, default=dict)
