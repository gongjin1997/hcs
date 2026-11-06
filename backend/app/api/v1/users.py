"""系统管理 - 用户、角色与权限、操作审计。"""
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import ACTIONS, Auditor, get_auditor
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.permissions import ALL, PERMISSIONS, normalize
from app.core.response import ApiResponse, BizError, ErrorCode, not_found, ok
from app.core.security import hash_password
from app.core.xlsx import filename_stamp, xlsx_response
from app.models import AuditLog, Contact, ContactGroup, HcsAccount, Resource, Role, User
from app.models.base import utcnow
from app.schemas.auth import ResetPasswordIn, RoleIn, RoleOut, RolePatchIn, UserAdminOut, UserCreateIn, UserPatchIn
from app.services import identity
from app.services.tenancy import tenant_map


class IdentityRoute(APIRoute):
    """身份参数错误只返回字段和消息，绝不回显输入密码。"""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
                raise BizError(ErrorCode.BAD_REQUEST, "用户参数校验失败", 400, errors) from None

        return safe_handler


router = APIRouter(prefix="/system", tags=["用户与权限"], route_class=IdentityRoute)


def _global_identity_scope(scope: Scope = Depends(get_scope)) -> Scope:
    """用户与角色属于全局身份配置，不能借租户内管理权限提升全局权限。"""
    if not scope.full:
        raise BizError(ErrorCode.FORBIDDEN, "用户与角色为全局配置，管理需要全部租户的数据权限", 403)
    return scope


# ---------------- 用户 ----------------
def _user_metadata(db: Session) -> dict:
    contacts = dict(db.execute(select(Contact.user_id, Contact.id).where(Contact.user_id.is_not(None))).all())
    counts = dict(db.execute(select(Resource.owner_user_id, func.count()).where(Resource.lifecycle == "ACTIVE")
                             .group_by(Resource.owner_user_id)).all())
    by_contact: dict[int, list[str]] = {}
    for group in db.scalars(select(ContactGroup)):
        for cid in set((group.member_ids or []) + (group.leader_ids or [])):
            by_contact.setdefault(cid, []).append(group.name)
    return {"contacts": contacts, "counts": counts, "groups": by_contact}


def _user_out(db: Session, u: User, roles: dict[int, Role] | None = None, metadata: dict | None = None) -> UserAdminOut:
    role = (roles or {}).get(u.role_id or 0) if roles is not None else (db.get(Role, u.role_id) if u.role_id else None)
    meta = metadata if metadata is not None else _user_metadata(db)
    contact_id = meta["contacts"].get(u.id)
    return UserAdminOut(id=u.id, username=u.username, display_name=u.display_name, email=u.email, role_id=u.role_id,
                        role_code=role.code if role else None, role_name=role.name if role else "未分配角色",
                        tenant_ids=None if u.tenant_ids is None else [int(x) for x in u.tenant_ids],
                        phone=u.phone, login_enabled=u.login_enabled, notify_enabled=u.notify_enabled,
                        has_password=bool(u.password_hash and u.password_hash != "!"),
                        contact_id=contact_id, owner_count=meta["counts"].get(u.id, 0),
                        groups=meta["groups"].get(contact_id, []),
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


@router.get("/users", response_model=ApiResponse[list[UserAdminOut]], summary="用户列表",
            dependencies=[Depends(_global_identity_scope)])
def list_users(db: Session = Depends(get_db), _: object = Depends(require_perm("user:manage"))):
    roles = {r.id: r for r in db.scalars(select(Role))}
    metadata = _user_metadata(db)
    return ok([_user_out(db, u, roles, metadata) for u in db.scalars(select(User).order_by(User.id))])


@router.get("/user-options", summary="人员选项（台账 / 通知引用统一用户）")
def user_options(tenant_id: int | None = Query(None, ge=1), include_disabled: bool = False,
                 db: Session = Depends(get_db),
                 scope: Scope = Depends(get_scope),
                 _: CurrentUser = Depends(require_perm(
                     "ledger:view", "notify:view", "user:manage", "report:view", "report:manage",
                     "report:config", "setting:manage", "tenant:manage", any_of=True))):
    if tenant_id is not None:
        scope.ensure(tenant_id, "租户")
    ids = [tenant_id] if tenant_id is not None else scope.tenant_ids
    return ok(identity.user_options(db, ids, include_disabled=include_disabled))


@router.get("/users/export", summary="导出用户 Excel（不含密码）", dependencies=[Depends(_global_identity_scope)])
def export_users(db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                 _: CurrentUser = Depends(require_perm("user:manage"))):
    from app.services import user_excel

    content = user_excel.export_users(db)
    audit.log("user.export", target_type="user", target_name="用户管理")
    return xlsx_response(content, f"用户管理_{filename_stamp(utcnow(), '%Y%m%d_%H%M')}.xlsx")


@router.get("/users/import/template", summary="下载用户导入 Excel 模板", dependencies=[Depends(_global_identity_scope)])
def user_template(db: Session = Depends(get_db), _: CurrentUser = Depends(require_perm("user:manage"))):
    from app.services import user_excel

    return xlsx_response(user_excel.template(db), "用户导入模板.xlsx")


@router.post("/users/import", summary="用户 Excel 导入（先预览，全部校验通过后原子写入）",
             dependencies=[Depends(_global_identity_scope)])
def import_users(file: UploadFile = File(...), dry_run: bool = Query(True), db: Session = Depends(get_db),
                 audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(require_perm("user:manage"))):
    from app.services import user_excel

    content = file.file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise BizError(ErrorCode.BAD_REQUEST, "文件不能超过 10 MB")
    result, todo = user_excel.parse(db, content, cu)
    out = result.as_dict()
    if dry_run:
        return ok({**out, "dry_run": True, "applied": 0})
    if out["error_count"]:
        raise BizError(ErrorCode.BAD_REQUEST, "存在校验错误，请修改后重新导入", 400, out)
    try:
        applied = user_excel.apply(db, todo, cu)
        audit.log("user.import", target_type="user", target_name="用户管理",
                  detail={"total": out["total"], "created": out["created"], "updated": out["updated"], "applied": applied})
    except Exception:
        db.rollback()
        raise
    return ok({**out, "dry_run": False, "applied": applied})


@router.post("/users", response_model=ApiResponse[UserAdminOut], summary="新增统一用户（登录 / 通知 / 角色 / 租户范围）",
             dependencies=[Depends(_global_identity_scope)])
def create_user(body: UserCreateIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                cu: CurrentUser = Depends(require_perm("user:manage"))):
    try:
        u = identity.create_user_record(db, body, cu)
    except IntegrityError:
        db.rollback()
        raise BizError(ErrorCode.CONFLICT, "账号或人员关联已存在，请刷新后重试", 409) from None
    audit.log("user.create", target_type="user", target_id=u.id, target_name=f"{u.display_name}（{u.username}）",
              detail={"role_id": u.role_id, "tenants": _tenant_names(db, u.tenant_ids), "login_enabled": u.login_enabled})
    return ok(_user_out(db, u))


@router.patch("/users/{user_id}", response_model=ApiResponse[UserAdminOut],
              summary="修改统一用户（自动同步联系人和负责人）", dependencies=[Depends(_global_identity_scope)])
def patch_user(user_id: int, body: UserPatchIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
               cu: CurrentUser = Depends(require_perm("user:manage"))):
    u = _get_user(db, user_id)
    changes = identity.update_user_record(db, u, body, cu)
    if changes:
        audit.log("user.update", target_type="user", target_id=u.id, target_name=f"{u.display_name}（{u.username}）",
                  detail={"changes": changes})
    else:
        db.commit()
    return ok(_user_out(db, u))


@router.post("/users/{user_id}/reset-password", summary="重置密码（同时解除锁定）",
             dependencies=[Depends(_global_identity_scope)])
def reset_password(user_id: int, body: ResetPasswordIn, db: Session = Depends(get_db),
                   audit: Auditor = Depends(get_auditor), _: object = Depends(require_perm("user:manage"))):
    u = _get_user(db, user_id)
    u.password_hash, u.failed_attempts, u.locked_until = hash_password(body.password), 0, None
    u.token_version += 1
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


@router.post("/roles", response_model=ApiResponse[RoleOut], summary="新增自定义角色",
             dependencies=[Depends(_global_identity_scope)])
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


@router.patch("/roles/{role_id}", response_model=ApiResponse[RoleOut], summary="修改角色（名称 / 说明 / 权限点）",
              dependencies=[Depends(_global_identity_scope)])
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


@router.delete("/roles/{role_id}", summary="删除自定义角色（内置角色、仍有用户的角色不能删除）",
               dependencies=[Depends(_global_identity_scope)])
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
