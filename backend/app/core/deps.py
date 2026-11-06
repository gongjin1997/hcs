"""鉴权与数据范围依赖。

- 功能权限：require_perm("alarm:handle")，缺少权限返回 403 / 40301
- 数据权限：get_scope —— 用户的租户范围 ∩ 请求头 X-Tenant-Id（顶部“租户切换”），越权访问返回 403；
  单条数据不在范围内时按“不存在”处理（404），避免泄露其他租户的数据是否存在
"""
import time
from collections.abc import Callable
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .db import SessionLocal, get_db
from .logging import bind_log_context
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
    token_version: int = 0
    token_id: str | None = None
    token_expires: int = 0

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
                       tenant_ids=None if user.tenant_ids is None else [int(t) for t in user.tenant_ids],
                       token_version=user.token_version)


def _session_user(db: Session, payload: dict, *, ticket: bool = False) -> CurrentUser:
    from app.models import RevokedToken, User

    try:
        uid = int(payload.get("uid", 0))
        version = int(payload.get("ver", -1))
        expires = int(payload.get("session_exp" if ticket else "exp", 0))
    except (ValueError, TypeError) as exc:
        raise _unauthorized() from exc
    user = db.get(User, uid)
    if user is None or user.username != payload.get("sub") or not user.enabled or not user.login_enabled:
        raise _unauthorized()
    jti = payload.get("sid" if ticket else "jti")
    if version != user.token_version or expires <= time.time() or (not ticket and not jti):
        raise _unauthorized("会话已失效，请重新登录")
    if jti and db.get(RevokedToken, jti) is not None:
        raise _unauthorized("会话已退出，请重新登录")
    return to_current(db, user).model_copy(update={"token_id": jti, "token_expires": expires})


def load_user(db: Session, token: str | None) -> CurrentUser:
    if not token:
        raise _unauthorized()
    try:
        payload = decode_token(token)
    except jwt.PyJWTError as e:
        raise _unauthorized() from e
    return _session_user(db, payload)


def user_from_token(token: str | None) -> CurrentUser:
    """无法使用依赖注入的场景。"""
    with SessionLocal() as db:
        return load_user(db, token)


def user_from_ticket(ticket: str) -> CurrentUser:
    """SSE：校验并作废一次性票据，再按票据里的身份查库（停用、改角色立即生效）。"""
    from .security import consume_sse_ticket

    try:
        payload = consume_sse_ticket(ticket)
    except jwt.PyJWTError as e:
        raise _unauthorized("实时推送票据无效或已使用，请刷新页面") from e
    with SessionLocal() as db:
        return _session_user(db, payload, ticket=True)


def refresh_session(user: CurrentUser) -> CurrentUser:
    """长连接每次发事件/心跳前重新验证会话及最新权限。"""
    with SessionLocal() as db:
        return _session_user(db, {"uid": user.id, "sub": user.username, "ver": user.token_version,
                                  "jti": user.token_id, "exp": user.token_expires})


def get_current_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> CurrentUser:
    token = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
    user = load_user(db, token)
    bind_log_context(user_id=user.id)
    return user


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
            raise BizError(ErrorCode.FORBIDDEN, "无权访问该租户的数据", 403, {"reason": "tenant_scope"})
        return Scope([tid], allowed is None, user)
    return Scope(None if allowed is None else list(allowed), allowed is None, user)


def get_scope(x_tenant_id: str | None = Header(default=None), user: CurrentUser = Depends(get_current_user)) -> Scope:
    """顶部“租户切换”通过请求头 X-Tenant-Id 传入（空 / all = 用户可见的全部租户）。"""
    scope = scope_of(user, x_tenant_id)
    bind_log_context(tenant_id=scope.single, tenant_scope=scope.tenant_ids)
    return scope
