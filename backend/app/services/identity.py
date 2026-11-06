"""人员唯一来源：用户档案、联系人投影和资源负责人。

所有函数参与调用方事务，不提交；导入预览只调用 validate_*，不会修改数据库。
"""
from typing import NoReturn

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser
from app.core.response import BizError, ErrorCode
from app.core.security import hash_password
from app.models import AlarmEvent, Contact, HcsAccount, Notification, Resource, Role, User
from app.models.alarm import ACTIVE_STATUSES
from app.schemas.auth import UserCreateIn, UserPatchIn

LABELS = {"display_name": "姓名", "email": "邮箱", "phone": "电话", "role_id": "角色", "tenant_ids": "租户范围",
          "enabled": "启用", "login_enabled": "允许登录", "notify_enabled": "接收通知"}


def _bad(message: str) -> NoReturn:
    raise BizError(ErrorCode.BAD_REQUEST, message)


def _manager(actor: CurrentUser) -> None:
    if actor.tenant_ids is not None or not actor.can("user:manage"):
        raise BizError(ErrorCode.FORBIDDEN, "人员管理需要全租户用户管理权限", 403)


def _tenants(db: Session, ids: list[int] | None, *, login: bool) -> list[int] | None:
    if ids is None:
        return None
    known = set(db.scalars(select(HcsAccount.id)))
    if any(isinstance(t, bool) or t not in known for t in ids):
        _bad("租户不存在")
    if login and not ids:
        _bad("请至少选择一个租户，或选择全部租户")
    return sorted(set(ids))


def _role(db: Session, role_id: int | None, *, login: bool) -> None:
    if role_id is None:
        if login:
            _bad("开通登录须分配角色")
        return
    if db.get(Role, role_id) is None:
        _bad("角色不存在")


def _email_available(db: Session, email: str | None, exclude: int | None = None) -> None:
    if not email:
        return
    q = select(User.id).where(func.lower(User.email) == email.lower())
    if exclude is not None:
        q = q.where(User.id != exclude)
    if db.scalar(q) is not None:
        raise BizError(ErrorCode.CONFLICT, "邮箱已被其他用户使用，请在用户管理中核对", 409)


def validate_user_create(db: Session, body: UserCreateIn, actor: CurrentUser) -> None:
    _manager(actor)
    if db.scalar(select(User.id).where(func.lower(User.username) == body.username.lower())) is not None:
        raise BizError(ErrorCode.CONFLICT, f"账号已存在：{body.username}", 409)
    _role(db, body.role_id, login=body.login_enabled)
    _tenants(db, body.tenant_ids, login=body.login_enabled)
    _email_available(db, body.email)


def _patch_values(user: User, body: UserPatchIn) -> dict:
    values = body.model_dump(exclude_unset=True, exclude={"all_tenants", "password"})
    if body.all_tenants:
        if body.tenant_ids is not None:
            _bad("全部租户与指定租户不能同时设置")
        values["tenant_ids"] = None
    elif "tenant_ids" in values and values["tenant_ids"] is None:
        # 使用显式 all_tenants 标志，避免 null 误扩大数据范围。
        _bad("设置全部租户请使用 all_tenants")
    return values


def _is_admin(db: Session, role_id: int | None) -> bool:
    return role_id is not None and db.scalar(select(Role.code).where(Role.id == role_id)) == "admin"


def validate_user_patch(db: Session, user: User, body: UserPatchIn, actor: CurrentUser) -> None:
    _manager(actor)
    values = _patch_values(user, body)
    login = values.get("login_enabled", user.login_enabled)
    enabled = values.get("enabled", user.enabled)
    role_id = values.get("role_id", user.role_id)
    scope = values.get("tenant_ids", user.tenant_ids)
    if body.password is not None and (user.login_enabled or not login or user.password_hash != "!"):
        _bad("已有登录账号请使用重置密码；初始密码仅用于首次开通登录")
    if login and not user.login_enabled and user.password_hash == "!" and not body.password:
        _bad("首次开通登录请设置初始密码")
    _role(db, role_id, login=login)
    _tenants(db, scope, login=login)
    if "email" in values and values["email"] != user.email:
        _email_available(db, values["email"], user.id)
    if user.id == actor.id:
        if not enabled or not login:
            _bad("不能停用自己的账号或关闭自己的登录")
        if actor.is_admin and not _is_admin(db, role_id):
            _bad("不能取消自己的平台管理员角色")
        if actor.tenant_ids is None and scope is not None:
            _bad("不能限制自己的租户范围")
    # 在角色行上串行化管理员身份减少操作；导入整批的最终状态另行校验。
    losing = (not enabled or not login or scope is not None or role_id != user.role_id)
    if losing and user.enabled and user.login_enabled and user.tenant_ids is None and _is_admin(db, user.role_id):
        role = db.scalar(select(Role).where(Role.code == "admin").with_for_update())
        assert role is not None
        others = db.scalars(select(User).where(User.role_id == role.id, User.id != user.id,
                                              User.enabled.is_(True), User.login_enabled.is_(True)))
        if not any(other.tenant_ids is None for other in others):
            raise BizError(ErrorCode.CONFLICT, "不能停用、降级或限制最后一位全租户平台管理员", 409)


def sync_contact(db: Session, user: User) -> Contact | None:
    """保留联系人 ID 及组引用，用户字段是唯一来源；清空邮箱只停用投影。"""
    db.flush()
    contact = db.scalar(select(Contact).where(Contact.user_id == user.id))
    if contact is None:
        if not user.email and not user.phone:
            return None
        contact = Contact(user_id=user.id)
        db.add(contact)
    contact.name, contact.email, contact.phone = user.display_name, user.email or "", user.phone
    contact.enabled = user.enabled and user.notify_enabled and bool(user.email or user.phone)
    db.flush()
    return contact


def _lock_directory(db: Session) -> None:
    # 人工维护与批量导入共用管理员角色锁，串行校验邮箱、账号和管理员保留条件。
    db.scalar(select(Role.id).where(Role.code == "admin").with_for_update())


def create_user_record(db: Session, body: UserCreateIn, actor: CurrentUser) -> User:
    _manager(actor)
    _lock_directory(db)
    validate_user_create(db, body, actor)
    user = User(username=body.username, display_name=body.display_name, email=body.email, phone=body.phone,
                enabled=body.enabled, login_enabled=body.login_enabled, notify_enabled=body.notify_enabled,
                role_id=body.role_id, tenant_ids=_tenants(db, body.tenant_ids, login=body.login_enabled),
                password_hash=hash_password(body.password) if body.password else "!")
    db.add(user)
    db.flush()
    sync_contact(db, user)
    return user


def update_user_record(db: Session, user: User, body: UserPatchIn, actor: CurrentUser) -> dict:
    _manager(actor)
    _lock_directory(db)
    if not db.is_modified(user, include_collections=False):
        db.refresh(user)
    validate_user_patch(db, user, body, actor)
    values = _patch_values(user, body)
    changes = {LABELS[key]: [getattr(user, key), value] for key, value in values.items() if getattr(user, key) != value}
    auth_changed = any(key in values and values[key] != getattr(user, key)
                       for key in ("enabled", "login_enabled", "role_id", "tenant_ids"))
    old_name, old_email = user.display_name, user.email
    for key, value in values.items():
        setattr(user, key, value)
    if body.password:
        user.password_hash = hash_password(body.password)
        user.failed_attempts, user.locked_until = 0, None
        auth_changed = True
        changes["初始密码"] = ["未设置", "已设置"]
    if auth_changed:
        user.token_version += 1
    db.flush()
    sync_contact(db, user)
    if old_email and old_email != user.email:
        # Outbox 旧记录只有邮箱快照；用户改邮箱后不能再向旧地址投递。
        for notice in db.scalars(select(Notification).where(Notification.status.in_(("PENDING", "RETRYING")))):
            recipients = [email for email in notice.recipients or [] if email.casefold() != old_email.casefold()]
            if len(recipients) != len(notice.recipients or []):
                notice.recipients = recipients
                if not recipients:
                    notice.status, notice.error, notice.next_retry_at = "MERGED", "用户邮箱已变更，旧收件地址通知已取消", None
    if old_name != user.display_name:
        refs = select(Resource.id).where(Resource.owner_user_id == user.id)
        db.execute(update(AlarmEvent).where(AlarmEvent.resource_ref.in_(refs),
                                           AlarmEvent.status.in_(ACTIVE_STATUSES)).values(owner=user.display_name))
        db.execute(update(Resource).where(Resource.owner_user_id == user.id).values(owner=user.display_name))
    return changes


def user_options(db: Session, tenant_ids: list[int] | None, *, include_disabled: bool = False) -> list[dict]:
    query = select(User).order_by(User.display_name, User.username)
    if not include_disabled:
        query = query.where(User.enabled.is_(True))
    users = db.scalars(query)
    return [{"id": u.id, "username": u.username, "display_name": u.display_name, "email": u.email, "phone": u.phone,
             "enabled": u.enabled, "notify_enabled": u.notify_enabled, "tenant_ids": u.tenant_ids}
            for u in users if tenant_ids is None or u.tenant_ids is None or set(tenant_ids).intersection(u.tenant_ids)]


def resolve_owner(db: Session, *, tenant_id: int | None, owner_user_id: int | None = None,
                  owner: str | None = None) -> User | None:
    if owner_user_id is None and not (owner or "").strip():
        return None
    if owner_user_id is not None:
        user = db.get(User, owner_user_id)
    else:
        text = (owner or "").strip()
        exact = list(db.scalars(select(User).where(User.username == text)))
        matches = exact or list(db.scalars(select(User).where(User.display_name == text)))
        if len(matches) > 1:
            _bad("负责人重名，请使用账号或从用户列表选择")
        user = matches[0] if matches else None
    if user is None or not user.enabled:
        _bad("负责人不存在或已停用，请在用户管理中维护")
    if user.tenant_ids is not None and (tenant_id is None or tenant_id not in user.tenant_ids):
        _bad("负责人未分配到该租户，请在用户管理中调整数据范围")
    return user


def contact_users(db: Session, *, enabled_only: bool = False) -> dict[int, User]:
    q = select(Contact.id, User).join(User, Contact.user_id == User.id)
    if enabled_only:
        q = q.where(User.enabled.is_(True), User.notify_enabled.is_(True), User.email.is_not(None), User.email != "")
    return {cid: user for cid, user in db.execute(q)}
