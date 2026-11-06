"""ECS 项目配额：GET 只读缓存；POST 显式查询云端并审计。由总路由单独注册。"""

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_auditor
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.response import ApiResponse, BizError, ErrorCode, ok
from app.schemas.ecs_quotas import EcsQuotaOut, EcsQuotasOut
from app.services.ecs_quotas import list_quotas, refresh_quota, resolve_target

router = APIRouter(prefix="/ecs/quotas", tags=["ECS 项目配额"])


@router.get("", response_model=ApiResponse[EcsQuotasOut], summary="ECS 项目配额缓存（不调用云端）")
def quotas(tenant_id: int | None = Query(None, ge=1), project_ref: int | None = Query(None, ge=1),
           page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=100),
           db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
           _: CurrentUser = Depends(require_perm("ledger:view"))):
    return ok(list_quotas(db, scope, tenant_id=tenant_id, project_ref=project_ref, page=page, page_size=page_size))


@router.post("/{project_ref}/refresh", response_model=ApiResponse[EcsQuotaOut], summary="显式刷新一个项目的 ECS 实际配额")
def refresh(project_ref: int = Path(ge=1), tenant_id: int | None = Query(None, ge=1),
            db: Session = Depends(get_db), scope: Scope = Depends(get_scope), audit: Auditor = Depends(get_auditor),
            user: CurrentUser = Depends(require_perm("ledger:sync", "task:run", any_of=True))):
    target = resolve_target(db, scope, project_ref, tenant_id)
    try:
        data = refresh_quota(db, target)
    except BizError:
        audit.log("ecs.quota.refresh", target_type="ecs_quota", target_id=target.project_ref,
                  tenant_id=target.tenant_id, result="FAILED", detail={"project_ref": target.project_ref})
        raise
    audit.log("ecs.quota.refresh", target_type="ecs_quota", target_id=target.project_ref,
              tenant_id=target.tenant_id, result="FAILED" if data.last_error else "SUCCESS",
              detail={"project_ref": target.project_ref, "project_id": target.project_id,
                      "source": data.source, "status": data.status, "error": data.last_error}, commit=False)
    db.commit()
    if data.last_error:
        raise BizError(ErrorCode.HCS_UPSTREAM, "ECS 配额刷新失败；保留上次成功值，详情见刷新状态", 502,
                       data.model_dump(mode="json"))
    return ok(data)
