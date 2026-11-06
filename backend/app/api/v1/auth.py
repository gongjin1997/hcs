from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_anon_auditor, get_auditor
from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user, to_current
from app.core.response import ApiResponse, BizError, ErrorCode, ok
from app.core.security import create_token, hash_password, verify_password
from app.models import HcsAccount, RevokedToken, User
from app.models.base import utcnow
from app.schemas.auth import LoginIn, LoginOut, PasswordIn, TenantBrief, UserOut
from app.services.tenancy import tenant_brief

router = APIRouter(prefix="/auth", tags=["认证"])


def user_out(db: Session, user: User) -> UserOut:
    cu = to_current(db, user)
    q = select(HcsAccount).order_by(HcsAccount.id)
    if cu.tenant_ids is not None:
        q = q.where(HcsAccount.id.in_(cu.tenant_ids or [-1]))
    return UserOut(id=user.id, username=user.username, display_name=user.display_name, email=user.email,
                   role_id=user.role_id, role_code=cu.role_code, role_name=cu.role_name, permissions=cu.permissions,
                   tenant_ids=cu.tenant_ids, tenants=[TenantBrief(**tenant_brief(t)) for t in db.scalars(q)],
                   is_admin=cu.is_admin)


@router.get("/login-options", summary="登录页选项（公开）：平台版本；演示环境附带演示账号")
def login_options(db: Session = Depends(get_db)):
    """演示账号只在“非 prod 且开启演示数据、演示用户仍存在”时返回；生产环境登录页不会出现任何账号提示。"""
    from app import __version__
    from app.core.permissions import BUILTIN_ROLES
    from app.services.seed import DEMO_HINTS

    s = get_settings()
    demo: list[dict] = []
    if s.env != "prod" and s.demo_seed:
        names = {u for u, _, _ in DEMO_HINTS}
        exist = set(db.scalars(select(User.username).where(User.username.in_(names), User.enabled.is_(True))))
        role_name = {r["code"]: r["name"] for r in BUILTIN_ROLES}
        demo = [{"username": u, "password": p, "role": role_name.get(r, r)} for u, p, r in DEMO_HINTS if u in exist]
    return ok({"version": __version__, "env": s.env if s.env != "prod" else None, "demo_accounts": demo})


@router.post("/login", response_model=ApiResponse[LoginOut], summary="账号密码登录")
def login(body: LoginIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_anon_auditor)):
    s, now, username = get_settings(), utcnow(), body.username.strip()
    user = db.scalar(select(User).where(User.username == username))
    if user is not None and user.locked_until and user.locked_until > now:
        minutes = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
        audit.log("auth.login_failed", username=username, operator=user.display_name, user_id=user.id,
                  result="FAILED", detail={"reason": "账号临时锁定"})
        raise BizError(ErrorCode.UNAUTHORIZED, f"密码错误次数过多，请 {minutes} 分钟后再试", 401)
    if user is None or not user.enabled or not user.login_enabled or not verify_password(body.password, user.password_hash):
        reason = "用户不存在" if user is None else ("账号已停用" if not user.enabled else "密码错误")
        if user is not None and user.enabled and user.login_enabled:
            user.failed_attempts += 1
            if user.failed_attempts >= s.login_max_failures:
                user.failed_attempts, user.locked_until = 0, now + timedelta(minutes=s.login_lock_minutes)
                reason += f"，已连续 {s.login_max_failures} 次，临时锁定 {s.login_lock_minutes} 分钟"
        audit.log("auth.login_failed", username=username, operator=user.display_name if user else username,
                  user_id=user.id if user else None, result="FAILED", detail={"reason": reason})
        raise BizError(ErrorCode.UNAUTHORIZED, "用户名或密码错误", 401)
    user.failed_attempts, user.locked_until, user.last_login_at = 0, None, now
    audit.log("auth.login", username=user.username, operator=user.display_name, user_id=user.id,
              detail={"remember": body.remember})
    expires = s.jwt_remember_days * 86400 if body.remember else s.jwt_expire_minutes * 60
    return ok(LoginOut(access_token=create_token(user.username, user.id, body.remember, user.token_version), expires_in=expires,
                       user=user_out(db, user)))


@router.get("/me", response_model=ApiResponse[UserOut], summary="当前用户（角色、权限点、可访问租户）")
def me(cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    user = db.get(User, cu.id)
    assert user is not None
    return ok(user_out(db, user))


@router.post("/password", summary="修改自己的密码")
def change_password(body: PasswordIn, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db),
                    audit: Auditor = Depends(get_auditor)):
    user = db.get(User, cu.id)
    if user is None or not verify_password(body.old_password, user.password_hash):
        audit.log("auth.password", target_type="user", target_id=cu.id, target_name=cu.username, result="FAILED",
                  detail={"reason": "原密码不正确"})
        raise BizError(ErrorCode.BAD_REQUEST, "原密码不正确")
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1
    audit.log("auth.password", target_type="user", target_id=cu.id, target_name=cu.username)
    return ok(message="密码已修改")


@router.post("/logout", summary="退出当前登录会话（撤销令牌）")
def logout(cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db),
           audit: Auditor = Depends(get_auditor)):
    db.execute(delete(RevokedToken).where(RevokedToken.expires_at <= utcnow()))
    if cu.token_id and db.get(RevokedToken, cu.token_id) is None:
        db.add(RevokedToken(jti=cu.token_id, user_id=cu.id,
                            expires_at=datetime.fromtimestamp(cu.token_expires, UTC)))
    audit.log("auth.logout", target_type="user", target_id=cu.id, target_name=cu.username)
    return ok(message="已退出登录")


def display_name(db: Session, cu: CurrentUser) -> str:
    return cu.display_name or cu.username
