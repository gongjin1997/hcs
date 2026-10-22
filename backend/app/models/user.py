from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JsonType, UtcDateTime

from .base import TimestampMixin, utcnow


class Role(TimestampMixin, Base):
    """角色 = 一组权限点（见 app.core.permissions.PERMISSIONS）。内置 4 个角色，可新增自定义角色。"""

    __tablename__ = "sys_role"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(String(255), default="")
    permissions: Mapped[list] = mapped_column(JsonType, default=list)     # ["alarm:view", ...]，"*" 表示全部
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)


class User(TimestampMixin, Base):
    """平台账号：功能权限看角色，数据权限看租户范围（tenant_ids 为空 = 全部租户）。"""

    __tablename__ = "sys_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(255))
    role_id: Mapped[int | None] = mapped_column(Integer, index=True)
    tenant_ids: Mapped[list | None] = mapped_column(JsonType, nullable=True)    # None = 全部租户
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)    # 连续输错次数，达到上限临时锁定
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime())
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


class AuditLog(Base):
    """操作审计：登录、权限与租户配置变更、告警处置、台账维护、报表发送等写操作都会记一条。"""

    __tablename__ = "audit_log"
    __table_args__ = (Index("idx_audit_at", "at"), Index("idx_audit_action", "action", "at"))
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    user_id: Mapped[int | None] = mapped_column(Integer)
    operator: Mapped[str] = mapped_column(String(64))                  # 显示名（邮件链接操作为收件人邮箱）
    username: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(48))                    # 例如 alarm.close，中文名见 app.core.audit.ACTIONS
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    target_name: Mapped[str | None] = mapped_column(String(255))
    tenant_id: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[str] = mapped_column(String(16), default="SUCCESS")  # SUCCESS / FAILED
    detail: Mapped[dict] = mapped_column(JsonType, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
