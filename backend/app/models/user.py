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
    """统一人员：角色决定登录权限；tenant_ids=None 为全部租户，[] 为未分配范围。"""

    __tablename__ = "sys_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(32))
    login_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    notify_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    password_hash: Mapped[str] = mapped_column(String(255))
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    role_id: Mapped[int | None] = mapped_column(Integer, index=True)
    tenant_ids: Mapped[list | None] = mapped_column(JsonType, nullable=True)    # None = 全部租户
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)    # 连续输错次数，达到上限临时锁定
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime())
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


class RevokedToken(Base):
    """会话撤销持久化：跨 API 进程和重启生效，不依赖 Redis 可用性。"""

    __tablename__ = "revoked_token"
    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)


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
