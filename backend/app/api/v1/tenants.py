"""系统管理 - 租户接入：平台以只读账号登录各 HCS 租户（用户名密码换 Token，或 AK/SK 签名）。"""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_auditor
from app.core.crypto import encrypt, mask
from app.core.db import get_db
from app.core.deps import Scope, get_scope, require_perm
from app.core.response import ApiResponse, BizError, ErrorCode, not_found, ok
from app.metrics import effective
from app.models import HcsAccount, HcsProject, Resource, User
from app.schemas.system import ProjectOut, TenantIn, TenantOut
from app.services.tenant_check import check_tenant

router = APIRouter(prefix="/system/tenants", tags=["租户接入"])


def tenant_out(db: Session, t: HcsAccount) -> TenantOut:
    projects = db.scalars(select(HcsProject).where(HcsProject.account_id == t.id).order_by(HcsProject.id))
    users = sum(1 for u in db.scalars(select(User)) if u.tenant_ids is not None and t.id in u.tenant_ids)
    return TenantOut(
        id=t.id, code=t.code, name=t.name, short_name=t.short_name or t.name, color=t.color,
        iam_endpoint=t.iam_endpoint, domain_name=t.domain_name, domain_id=t.domain_id, auth_type=t.auth_type,
        username=t.username, access_key_mask=mask(t.access_key) if t.access_key else None,
        secret_set=bool(t.secret_cipher), endpoints=t.endpoints or {}, enabled=t.enabled,
        report_mail_to=t.report_mail_to or [], remark=t.remark, metrics_source=t.metrics_source or "AUTO",
        metrics_effective=effective(t), prometheus_url=t.prometheus_url, capabilities=t.capabilities or {},
        last_check=t.last_check or {}, last_check_at=t.last_check_at,
        projects=[ProjectOut(id=p.id, region_id=p.region_id, project_id=p.project_id, project_name=p.project_name,
                             external_domain=p.external_domain, env=p.env, workspaces=p.workspaces or [],
                             enabled=p.enabled) for p in projects],
        resource_count=db.scalar(select(func.count()).select_from(Resource).where(
            Resource.tenant_id == t.id, Resource.lifecycle == "ACTIVE")) or 0,
        user_count=users)


def _get(db: Session, scope: Scope, tenant_id: int) -> HcsAccount:
    t = db.get(HcsAccount, tenant_id)
    if t is None:
        raise not_found("租户")
    scope.ensure(t.id, "租户")
    return t


def _validate(body: TenantIn, creating: bool, existing: HcsAccount | None = None) -> None:
    if body.auth_type == "PASSWORD" and not body.username:
        raise BizError(ErrorCode.BAD_REQUEST, "用户名密码方式需要填写 IAM 用户名")
    # 修改时 AK 留空 = 沿用原 AK（列表只返回脱敏 AK，前端无法回填原值）
    keep_ak = existing is not None and existing.auth_type == "AKSK" and bool(existing.access_key)
    if body.auth_type == "AKSK" and not body.access_key and not keep_ak:
        raise BizError(ErrorCode.BAD_REQUEST, "AK/SK 方式需要填写 Access Key（AK）")
    if creating and not body.secret:
        raise BizError(ErrorCode.BAD_REQUEST, "请填写密码或 Secret Key（SK）")
    if body.metrics_source == "PROMETHEUS" and not body.prometheus_url:
        raise BizError(ErrorCode.BAD_REQUEST, "指标来源选择 Prometheus 时需要填写 Prometheus 地址")


def _apply(db: Session, t: HcsAccount, body: TenantIn) -> list[str]:
    changed = []
    fields = ("code", "name", "short_name", "color", "iam_endpoint", "domain_name", "domain_id", "auth_type",
              "username", "access_key", "endpoints", "enabled", "report_mail_to", "remark", "metrics_source",
              "prometheus_url")
    for f in fields:
        v = getattr(body, f)
        if f == "access_key" and body.auth_type != "AKSK":
            v = None
        elif f == "access_key" and not v:
            v = t.access_key                         # 留空 = 不修改
        if f == "username" and body.auth_type != "PASSWORD":
            v = None
        if getattr(t, f, None) != v:
            setattr(t, f, v)
            changed.append(f)
    if body.secret:
        t.secret_cipher = encrypt(body.secret)
        changed.append("secret")
    if "prometheus_url" in changed:                 # 地址变化后重新探测
        t.capabilities = {k: v for k, v in (t.capabilities or {}).items() if k != "prometheus"}
    db.flush()
    existing = {p.id: p for p in db.scalars(select(HcsProject).where(HcsProject.account_id == t.id))}
    by_key = {(p.region_id, p.project_id): p for p in existing.values()}
    keep = set()
    for pin in body.projects:
        # 按 id 匹配；没带 id 时按（region, project_id）匹配已有项目，避免重复插入
        p = existing.get(pin.id) if pin.id else by_key.get((pin.region_id, pin.project_id))
        if p is None:
            p = HcsProject(account_id=t.id)
            db.add(p)
        for f in ("region_id", "project_id", "project_name", "external_domain", "env", "workspaces", "enabled"):
            setattr(p, f, getattr(pin, f))
        if p.id:
            keep.add(p.id)
    for pid, p in existing.items():
        if pid not in keep:
            if db.scalar(select(Resource.id).where(Resource.project_ref == pid).limit(1)) is not None:
                p.enabled = False                    # 已有台账数据的项目只停用，不删除
            else:
                db.delete(p)
    return changed


@router.get("", response_model=ApiResponse[list[TenantOut]], summary="租户列表（密钥脱敏）")
def list_tenants(db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                 _: object = Depends(require_perm("tenant:view"))):
    q = scope.apply(select(HcsAccount).order_by(HcsAccount.id), HcsAccount.id)
    return ok([tenant_out(db, t) for t in db.scalars(q)])


@router.post("", response_model=ApiResponse[TenantOut], summary="新增租户")
def create_tenant(body: TenantIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                  _: object = Depends(require_perm("tenant:manage"))):
    _validate(body, creating=True)
    for col, v, what in ((HcsAccount.code, body.code, "租户代码"), (HcsAccount.name, body.name, "租户名称")):
        if db.scalar(select(HcsAccount.id).where(col == v)) is not None:
            raise BizError(ErrorCode.CONFLICT, f"{what}已存在：{v}", 409)
    t = HcsAccount(code=body.code, name=body.name, iam_endpoint=body.iam_endpoint, domain_name=body.domain_name,
                   secret_cipher="", endpoints={}, report_mail_to=[], capabilities={}, last_check={})
    db.add(t)
    db.flush()
    _apply(db, t, body)
    audit.log("tenant.create", target_type="tenant", target_id=t.id, target_name=t.name, tenant_id=t.id,
              detail={"auth_type": t.auth_type, "projects": len(body.projects)})
    return ok(tenant_out(db, t))


@router.put("/{tenant_id}", response_model=ApiResponse[TenantOut], summary="修改租户（密码 / SK 留空表示不修改）")
def update_tenant(tenant_id: int, body: TenantIn, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                  audit: Auditor = Depends(get_auditor), _: object = Depends(require_perm("tenant:manage"))):
    t = _get(db, scope, tenant_id)
    _validate(body, creating=False, existing=t)
    if body.auth_type != t.auth_type and not body.secret:
        raise BizError(ErrorCode.BAD_REQUEST, "切换认证方式时需要重新填写密码或 SK")
    dup = db.scalar(select(HcsAccount.id).where((HcsAccount.code == body.code) | (HcsAccount.name == body.name),
                                                HcsAccount.id != t.id))
    if dup is not None:
        raise BizError(ErrorCode.CONFLICT, "租户代码或名称与其他租户重复", 409)
    changed = _apply(db, t, body)
    audit.log("tenant.update", target_type="tenant", target_id=t.id, target_name=t.name, tenant_id=t.id,
              detail={"fields": changed})
    return ok(tenant_out(db, t))


@router.delete("/{tenant_id}", summary="删除租户（已有台账数据的租户不能删除，可停用）")
def delete_tenant(tenant_id: int, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                  audit: Auditor = Depends(get_auditor), _: object = Depends(require_perm("tenant:manage"))):
    t = _get(db, scope, tenant_id)
    n = db.scalar(select(func.count()).select_from(Resource).where(Resource.tenant_id == t.id)) or 0
    if n:
        raise BizError(ErrorCode.CONFLICT, f"租户下还有 {n} 条台账 / 巡检数据，不能删除；如不再巡检请停用该租户", 409)
    for p in db.scalars(select(HcsProject).where(HcsProject.account_id == t.id)):
        db.delete(p)
    db.delete(t)
    audit.log("tenant.delete", target_type="tenant", target_id=tenant_id, target_name=t.name, tenant_id=tenant_id)
    return ok(message="已删除")


@router.post("/{tenant_id}/test", summary="连通性测试 + 能力探测（IAM 认证、各服务接口、CES / Prometheus / CCE K8s API）")
def test_tenant(tenant_id: int, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                audit: Auditor = Depends(get_auditor), _: object = Depends(require_perm("tenant:manage"))):
    t = _get(db, scope, tenant_id)
    result = check_tenant(db, t)
    audit.log("tenant.test", target_type="tenant", target_id=t.id, target_name=t.name, tenant_id=t.id,
              result="SUCCESS" if result["ok"] else "FAILED",
              detail={"failed": [c["name"] for c in result["checks"] if not c["ok"]],
                      "metrics_source": result["metrics_source"]})
    return ok({**result, "tenant": tenant_out(db, t).model_dump(mode="json")})
