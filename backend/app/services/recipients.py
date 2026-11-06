"""通知收件人只引用统一用户目录；邮箱是运行时投影，不创建人员或联系人。"""

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.response import BizError, ErrorCode
from app.models import User

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_MANAGE = "收件人员不存在、未启用通知、邮箱无效或不属于当前租户，请在用户管理中维护并选择人员"


@dataclass(frozen=True)
class _Recipient:
    id: int
    email: str | None
    enabled: bool
    notify_enabled: bool
    tenant_ids: list[int] | None


def _users(db: Session, ids: list[int] | None = None) -> list[_Recipient]:
    if ids == []:
        return []
    query = select(User.id, User.email, User.enabled, User.notify_enabled, User.tenant_ids).order_by(User.id)
    if ids is not None:
        query = query.where(User.id.in_(ids))
    # 读取列而非 ORM identity map，长期运行的 worker 也能拿到最新开关和邮箱。
    with db.no_autoflush:
        return [_Recipient(*row) for row in db.execute(query)]


def _in_scope(user: _Recipient, tenant_id: int | None) -> bool:
    return user.tenant_ids is None or (tenant_id is not None and tenant_id in user.tenant_ids)


def _email(user: _Recipient) -> str | None:
    email = (user.email or "").strip()
    return email if _EMAIL.fullmatch(email) else None


def _eligible(user: _Recipient, tenant_id: int | None, *, allow_any_scope: bool = False) -> bool:
    return user.enabled and user.notify_enabled and _email(user) is not None and (allow_any_scope or _in_scope(user, tenant_id))


def validate_user_ids(db: Session, ids: list[int], *, tenant_id: int | None = None,
                      existing_ids: list[int] | None = None, allow_any_scope: bool = False) -> list[int]:
    """新增选择须可投递；原选择允许保留，运行时另行过滤失效人员。"""
    if not isinstance(ids, list) or any(type(uid) is not int or uid <= 0 for uid in ids):
        raise BizError(ErrorCode.BAD_REQUEST, "收件人员 ID 必须为正整数，请在用户管理中选择人员")
    normalized = list(dict.fromkeys(ids))
    existing = {uid for uid in (existing_ids or []) if type(uid) is int and uid > 0}
    users = {user.id: user for user in _users(db, normalized)}
    for uid in normalized:
        if uid not in existing and (uid not in users or not _eligible(users[uid], tenant_id, allow_any_scope=allow_any_scope)):
            raise BizError(ErrorCode.BAD_REQUEST, _MANAGE)
    return normalized


def emails_for_users(db: Session, ids: list[int], *, tenant_id: int | None = None,
                     allow_any_scope: bool = False) -> list[str]:
    """按选择顺序投影当前邮箱；禁用、无邮箱及越界人员不投递。"""
    selected = list(dict.fromkeys(uid for uid in ids if type(uid) is int and uid > 0))
    users = {user.id: user for user in _users(db, selected)}
    emails: list[str] = []
    seen: set[str] = set()
    for uid in selected:
        user = users.get(uid)
        if user is None or not _eligible(user, tenant_id, allow_any_scope=allow_any_scope):
            continue
        email = _email(user)
        assert email is not None
        if email.casefold() not in seen:
            emails.append(email)
            seen.add(email.casefold())
    return emails


def ids_for_emails(db: Session, emails: list[str], *, tenant_id: int | None = None, strict: bool = True,
                   allow_any_scope: bool = False) -> list[int]:
    """旧邮箱配置只能解析到范围内唯一的已有人员；宽松读取跳过未知和歧义地址。"""
    if not isinstance(emails, list):
        if strict:
            raise BizError(ErrorCode.BAD_REQUEST, "收件邮箱必须为列表，请在用户管理中选择人员")
        return []
    matches: dict[str, list[int]] = {}
    for user in _users(db):
        email = _email(user)
        if email is not None and (allow_any_scope or _in_scope(user, tenant_id)):
            matches.setdefault(email.casefold(), []).append(user.id)
    out: list[int] = []
    for email in emails:
        if not isinstance(email, str) or not _EMAIL.fullmatch(email.strip()):
            if strict:
                raise BizError(ErrorCode.BAD_REQUEST, "收件邮箱格式不正确，请在用户管理中维护并选择人员")
            continue
        candidates = matches.get(email.strip().casefold(), [])
        if len(candidates) != 1:
            if strict:
                raise BizError(ErrorCode.BAD_REQUEST, "收件邮箱未匹配唯一人员或不属于当前租户，请在用户管理中维护并选择人员")
            continue
        if candidates[0] not in out:
            out.append(candidates[0])
    return out
