"""系统管理 - 用户、角色与权限、操作审计。"""
from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.audit import ACTIONS, Auditor, get_auditor
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.permissions import ALL, PERMISSIONS, normalize
from app.core.response import ApiResponse, BizError, ErrorCode, not_found, ok
from app.core.security import hash_password
from app.models import AuditLog, HcsAccount, Role, User
from app.models.base import utcnow
from app.schemas.auth import ResetPasswordIn, RoleIn, RoleOut, RolePatchIn, UserAdminOut, UserCreateIn, UserPatchIn
from app.services.tenancy import tenant_map

router = APIRouter(prefix="/system", tags=["用户与权限"])


# ---------------- 用户 ----------------
def _user_out(db: Session, u: User, roles: dict[int, Role] | None = None) -> UserAdminOut:
    role = (roles or {}).get(u.role_id or 0) if roles is not None else (db.get(Role, u.role_id) if u.role_id else None)
    return UserAdminOut(id=u.id, username=u.username, display_name=u.display_name, email=u.email, role_id=u.role_id,
                        role_code=role.code if role else None, role_name=role.name if role else "未分配角色",
                        tenant_ids=None if u.tenant_ids is None else [int(x) for x in u.tenant_ids],
                        enabled=u.enabled, last_login_at=u.last_login_at, created_at=u.created_at,
                        locked=bool(u.locked_until and u.locked_until > utcnow()))


def _get_user(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if u is None:
        raise not_found("用户")
    return u


def _check_role(db: Session, role_id: int) -> Role:
    r = db.get(Role, role_id)
    if r is None:
        raise BizError(ErrorCode.BAD_REQUEST, "角色不存在")
    return r


def _check_tenants(db: Session, ids: list[int] | None) -> list[int] | None:
    if ids is None:
        return None
    known = set(tenant_map(db))
    bad = [i for i in ids if i not in known]
    if bad:
        raise BizError(ErrorCode.BAD_REQUEST, f"租户不存在：{bad}")
    if not ids:
        raise BizError(ErrorCode.BAD_REQUEST, "请至少选择一个租户，或选择“全部租户”")
    return sorted(set(ids))


def _tenant_names(db: Session, ids: list[int] | None) -> str:
    if ids is None:
        return "全部租户"
    tm = tenant_map(db)
    return "、".join((tm[i].short_name or tm[i].name) if i in tm else str(i) for i in ids)


@router.get("/users", response_model=ApiResponse[list[UserAdminOut]], summary="用户列表")
def list_users(db: Session = Depends(get_db), _: object = Depends(require_perm("user:manage"))):
    roles = {r.id: r for r in db.scalars(select(Role))}
    return ok([_user_out(db, u, roles) for u in db.scalars(select(User).order_by(User.id))])


@router.post("/users", response_model=ApiResponse[UserAdminOut], summary="新增用户（角色 + 租户范围）")
def create_user(body: UserCreateIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                _: object = Depends(require_perm("user:manage"))):
    if db.scalar(select(User.id).where(User.username == body.username)):
        raise BizError(ErrorCode.CONFLICT, f"用户名已存在：{body.username}", 409)
    role = _check_role(db, body.role_id)
    tenants = _check_tenants(db, body.tenant_ids)
    u = User(username=body.username, display_name=body.display_name, email=body.email, role_id=role.id,
             tenant_ids=tenants, password_hash=hash_password(body.password))
    db.add(u)
    db.flush()
    audit.log("user.create", target_type="user", target_id=u.id, target_name=f"{u.display_name}（{u.username}）",
              detail={"role": role.name, "tenants": _tenant_names(db, tenants)})
    return ok(_user_out(db, u))


@router.patch("/users/{user_id}", response_model=ApiResponse[UserAdminOut],
              summary="修改用户（姓名 / 邮箱 / 角色 / 租户范围 / 启停）")
def patch_user(user_id: int, body: UserPatchIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
               cu: CurrentUser = Depends(require_perm("user:manage"))):
    u = _get_user(db, user_id)
    changes: dict[str, list] = {}
    if body.enabled is False and u.id == cu.id:
        raise BizError(ErrorCode.BAD_REQUEST, "不能停用自己的账号")
    if body.role_id is not None and body.role_id != u.role_id:
        role = _check_role(db, body.role_id)
        if u.id == cu.id and cu.is_admin and role.code != "admin":
            raise BizError(ErrorCode.BAD_REQUEST, "不能取消自己的平台管理员角色")
        old = db.get(Role, u.role_id) if u.role_id else None
        changes["角色"] = [old.name if old else None, role.name]
        u.role_id = role.id
    if body.all_tenants:
        if u.tenant_ids is not None:
            changes["租户范围"] = [_tenant_names(db, u.tenant_ids), "全部租户"]
        u.tenant_ids = None
    elif body.tenant_ids is not None:
        new = _check_tenants(db, body.tenant_ids)
        if new != u.tenant_ids:
            changes["租户范围"] = [_tenant_names(db, u.tenant_ids), _tenant_names(db, new)]
        u.tenant_ids = new
    for f, label in (("display_name", "姓名"), ("email", "邮箱"), ("enabled", "启用")):
        v = getattr(body, f)
        if v is not None and v != getattr(u, f):
            changes[label] = [getattr(u, f), v]
            setattr(u, f, v)
    if changes:
        audit.log("user.update", target_type="user", target_id=u.id, target_name=f"{u.display_name}（{u.username}）",
                  detail={"changes": changes})
    else:
        db.commit()
    return ok(_user_out(db, u))


@router.post("/users/{user_id}/reset-password", summary="重置密码（同时解除锁定）")
def reset_password(user_id: int, body: ResetPasswordIn, db: Session = Depends(get_db),
                   audit: Auditor = Depends(get_auditor), _: object = Depends(require_perm("user:manage"))):
    u = _get_user(db, user_id)
    u.password_hash, u.failed_attempts, u.locked_until = hash_password(body.password), 0, None
    audit.log("user.reset_password", target_type="user", target_id=u.id, target_name=f"{u.display_name}（{u.username}）")
    return ok(message="密码已重置")


# ---------------- 角色与权限 ----------------
@router.get("/permissions", summary="全部权限点（按模块分组）")
def permissions(_: object = Depends(require_perm("role:manage", "user:manage", any_of=True))):
    groups: dict[str, list] = {}
    for p in PERMISSIONS:
        groups.setdefault(p["group"], []).append({"code": p["code"], "name": p["name"]})
    return ok([{"group": g, "items": items} for g, items in groups.items()])


def _role_out(r: Role, counts: dict[int, int]) -> RoleOut:
    return RoleOut(id=r.id, code=r.code, name=r.name, description=r.description or "",
                   permissions=list(r.permissions or []), builtin=r.builtin, user_count=counts.get(r.id, 0))


def _counts(db: Session) -> dict[int, int]:
    return dict(db.execute(select(User.role_id, func.count()).group_by(User.role_id)).all())  # type: ignore[arg-type]


@router.get("/roles", response_model=ApiResponse[list[RoleOut]], summary="角色列表")
def list_roles(db: Session = Depends(get_db), _: object = Depends(require_perm("role:manage", "user:manage",
                                                                                 any_of=True))):
    counts = _counts(db)
    return ok([_role_out(r, counts) for r in db.scalars(select(Role).order_by(Role.builtin.desc(), Role.id))])


def _perms(perms: list[str]) -> list[str]:
    try:
        return normalize(perms)
    except ValueError as e:
        raise BizError(ErrorCode.BAD_REQUEST, str(e)) from e


@router.post("/roles", response_model=ApiResponse[RoleOut], summary="新增自定义角色")
def create_role(body: RoleIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                _: object = Depends(require_perm("role:manage"))):
    if db.scalar(select(Role.id).where(Role.code == body.code)) is not None:
        raise BizError(ErrorCode.CONFLICT, f"角色编码已存在：{body.code}", 409)
    perms = _perms(body.permissions)
    if ALL in perms:
        raise BizError(ErrorCode.BAD_REQUEST, "只有平台管理员角色可以拥有全部权限（*）")
    r = Role(code=body.code, name=body.name, description=body.description, permissions=perms, builtin=False)
    db.add(r)
    db.flush()
    audit.log("role.create", target_type="role", target_id=r.id, target_name=r.name,
              detail={"permissions": r.permissions})
    return ok(_role_out(r, {}))


@router.patch("/roles/{role_id}", response_model=ApiResponse[RoleOut], summary="修改角色（名称 / 说明 / 权限点）")
def patch_role(role_id: int, body: RolePatchIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
               _: object = Depends(require_perm("role:manage"))):
    r = db.get(Role, role_id)
    if r is None:
        raise not_found("角色")
    detail: dict = {}
    if body.permissions is not None:
        if r.code == "admin":
            raise BizError(ErrorCode.BAD_REQUEST, "平台管理员拥有全部权限，不能修改")
        new = _perms(body.permissions)
        if ALL in new:
            raise BizError(ErrorCode.BAD_REQUEST, "只有平台管理员角色可以拥有全部权限（*）")
        old = set(r.permissions or [])
        detail = {"added": sorted(set(new) - old), "removed": sorted(old - set(new))}
        r.permissions = new
    if body.name is not None:
        r.name = body.name
    if body.description is not None:
        r.description = body.description
    audit.log("role.update", target_type="role", target_id=r.id, target_name=r.name, detail=detail)
    return ok(_role_out(r, _counts(db)))


@router.delete("/roles/{role_id}", summary="删除自定义角色（内置角色、仍有用户的角色不能删除）")
def delete_role(role_id: int, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                _: object = Depends(require_perm("role:manage"))):
    r = db.get(Role, role_id)
    if r is None:
        raise not_found("角色")
    if r.builtin:
        raise BizError(ErrorCode.BAD_REQUEST, "内置角色不能删除")
    n = _counts(db).get(r.id, 0)
    if n:
        raise BizError(ErrorCode.CONFLICT, f"还有 {n} 个用户使用该角色，请先调整这些用户的角色", 409)
    db.delete(r)
    audit.log("role.delete", target_type="role", target_id=role_id, target_name=r.name)
    return ok(message="已删除")


# ---------------- 操作审计 ----------------
@router.get("/audit-logs", summary="操作审计（按操作类型 / 关键字 / 结果 / 时间筛选）")
def audit_logs(action: list[str] | None = Query(default=None), keyword: str | None = None, result: str | None = None,
               days: int = Query(default=7, ge=1, le=180), page: int = Query(default=1, ge=1),
               page_size: int = Query(default=20, ge=1, le=200), db: Session = Depends(get_db),
               scope: Scope = Depends(get_scope), _: object = Depends(require_perm("audit:view"))):
    q = select(AuditLog).where(AuditLog.at >= utcnow() - timedelta(days=days))
    q = scope.apply(q, AuditLog.tenant_id, include_global=True)
    if action:
        conds = [AuditLog.action.like(a[:-1] + "%") if a.endswith("*") else AuditLog.action == a for a in action]
        q = q.where(or_(*conds))
    if result:
        q = q.where(AuditLog.result == result)
    if keyword:
        like = f"%{keyword.strip()}%"
        q = q.where(or_(AuditLog.operator.like(like), AuditLog.username.like(like), AuditLog.target_name.like(like),
                        AuditLog.ip.like(like)))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(AuditLog.at.desc(), AuditLog.id.desc()).offset((page - 1) * page_size)
                      .limit(page_size)).all()
    tm = {t.id: t for t in db.scalars(select(HcsAccount))}
    items = [{"id": a.id, "at": a.at, "operator": a.operator, "username": a.username, "action": a.action,
              "action_name": ACTIONS.get(a.action, a.action), "target_type": a.target_type, "target_id": a.target_id,
              "target_name": a.target_name, "tenant_id": a.tenant_id,
              "tenant_name": (tm[a.tenant_id].short_name or tm[a.tenant_id].name) if a.tenant_id in tm else None,
              "result": a.result, "detail": a.detail or {}, "ip": a.ip} for a in rows]
    return ok({"items": items, "total": total, "page": page, "page_size": page_size,
               "actions": [{"code": k, "name": v} for k, v in ACTIONS.items()]})
