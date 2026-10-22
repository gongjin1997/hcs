"""鉴权与数据范围依赖。

- 功能权限：require_perm("alarm:handle")，缺少权限返回 403 / 40301
- 数据权限：get_scope —— 用户的租户范围 ∩ 请求头 X-Tenant-Id（顶部“租户切换”），越权访问返回 403；
  单条数据不在范围内时按“不存在”处理（404），避免泄露其他租户的数据是否存在
"""
from collections.abc import Callable
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .db import SessionLocal, get_db
from .permissions import PERMISSION_NAMES, expand
from .response import BizError, ErrorCode, not_found
from .security import decode_token


class CurrentUser(BaseModel):
    id: int
    username: str
    display_name: str
    role_code: str | None = None
    role_name: str = "未分配角色"
    permissions: list[str] = []
    tenant_ids: list[int] | None = None       # None = 全部租户

    @property
    def is_admin(self) -> bool:
        return self.role_code == "admin"

    def can(self, code: str) -> bool:
        return code in self.permissions


def _unauthorized(msg: str = "未登录或登录已过期") -> BizError:
    return BizError(ErrorCode.UNAUTHORIZED, msg, 401)


def to_current(db: Session, user) -> CurrentUser:
    from app.models import Role

    role = db.get(Role, user.role_id) if user.role_id else None
    return CurrentUser(id=user.id, username=user.username, display_name=user.display_name,
                       role_code=role.code if role else None, role_name=role.name if role else "未分配角色",
                       permissions=expand(role.permissions if role else []),
                       tenant_ids=None if user.tenant_ids is None else [int(t) for t in user.tenant_ids])


def load_user(db: Session, token: str | None) -> CurrentUser:
    from app.models import User  # 延迟导入，避免 core ↔ models 循环依赖

    if not token:
        raise _unauthorized()
    try:
        payload = decode_token(token)
    except jwt.PyJWTError as e:
        raise _unauthorized() from e
    user = db.get(User, int(payload.get("uid", 0)))
    if user is None or user.username != payload.get("sub"):
        raise _unauthorized()
    if not user.enabled:
        raise _unauthorized("账号已停用，请联系管理员")
    return to_current(db, user)       # 角色 / 租户范围每次请求查库，管理员调整后立即生效


def user_from_token(token: str | None) -> CurrentUser:
    """无法使用依赖注入的场景（SSE 的 Query Token）。"""
    with SessionLocal() as db:
        return load_user(db, token)


def get_current_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> CurrentUser:
    token = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
    return load_user(db, token)


def forbidden(codes: tuple[str, ...]) -> BizError:
    names = "、".join(PERMISSION_NAMES.get(c, c) for c in codes)
    return BizError(ErrorCode.FORBIDDEN, f"缺少权限：{names}", 403, {"required": list(codes)})


def require_perm(*codes: str, any_of: bool = False) -> Callable[..., CurrentUser]:
    """路由依赖：Depends(require_perm("alarm:handle"))。默认要求全部权限点，any_of=True 时满足其一即可。"""

    def dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        ok = any(user.can(c) for c in codes) if any_of else all(user.can(c) for c in codes)
        if not ok:
            raise forbidden(codes)
        return user

    return dep


@dataclass
class Scope:
    """本次请求的数据范围。tenant_ids=None 表示不过滤（全部租户）；full 表示用户本身拥有全部租户权限。"""

    tenant_ids: list[int] | None
    full: bool
    user: CurrentUser

    def allows(self, tenant_id: int | None) -> bool:
        if tenant_id is None:                 # 不属于任何租户的数据（例如“全部租户”合并报表）只有全量用户可见
            return self.full
        return self.tenant_ids is None or tenant_id in self.tenant_ids

    def where(self, column, include_global: bool = False):
        """SQLAlchemy 过滤条件；None 表示无需过滤。include_global=True 时全量用户额外可见 tenant_id 为空的数据。"""
        if self.tenant_ids is None:
            return None
        cond = column.in_(self.tenant_ids)
        return or_(cond, column.is_(None)) if include_global and self.full else cond

    def apply(self, q, column, include_global: bool = False):
        cond = self.where(column, include_global)
        return q if cond is None else q.where(cond)

    def ensure(self, tenant_id: int | None, what: str) -> None:
        if not self.allows(tenant_id):
            raise not_found(what)

    @property
    def single(self) -> int | None:
        return self.tenant_ids[0] if self.tenant_ids and len(self.tenant_ids) == 1 else None


def scope_of(user: CurrentUser, tenant: str | int | None) -> Scope:
    allowed = user.tenant_ids
    if tenant not in (None, "", "all", "0", 0):
        try:
            tid = int(tenant)  # type: ignore[arg-type]
        except (TypeError, ValueError) as e:
            raise BizError(ErrorCode.BAD_REQUEST, "租户参数格式不正确") from e
        if allowed is not None and tid not in allowed:
            raise BizError(ErrorCode.FORBIDDEN, "无权访问该租户的数据", 403)
        return Scope([tid], allowed is None, user)
    return Scope(None if allowed is None else list(allowed), allowed is None, user)


def get_scope(x_tenant_id: str | None = Header(default=None), user: CurrentUser = Depends(get_current_user)) -> Scope:
    """顶部“租户切换”通过请求头 X-Tenant-Id 传入（空 / all = 用户可见的全部租户）。"""
    return scope_of(user, x_tenant_id)
