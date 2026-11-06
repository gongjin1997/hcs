"""Tenant ECS collection policies. Router registration is owned by the application."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_auditor
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.response import ApiResponse, BizError, ErrorCode, not_found, ok
from app.models import HcsAccount
from app.schemas.collection_policy import CollectionPolicy, CollectionPolicyOut, CollectionPolicySaveOut, CollectionPolicyUpdate
from app.services import collect_switch, collection_policy

router = APIRouter(prefix="/system/collection-policies", tags=["采集策略"])


def _check(db: Session, scope: Scope, tenant_id: int, resource_type: str) -> None:
    if tenant_id <= 0:
        raise BizError(ErrorCode.BAD_REQUEST, "采集策略须指定租户，不支持全局策略")
    scope.ensure(tenant_id, "租户采集策略")
    if db.get(HcsAccount, tenant_id) is None:
        raise not_found("租户")
    if resource_type != collection_policy.RESOURCE_TYPE:
        raise BizError(ErrorCode.BAD_REQUEST, "当前采集策略仅支持 ECS 云服务器")


@router.get("/{tenant_id}/{resource_type}", summary="查看租户 ECS 独立阶段采集策略",
            response_model=ApiResponse[CollectionPolicyOut])
def get_policy(tenant_id: int, resource_type: str, db: Session = Depends(get_db),
               scope: Scope = Depends(get_scope),
               _: CurrentUser = Depends(require_perm("ledger:view", "task:view", any_of=True))):
    _check(db, scope, tenant_id, resource_type)
    policy = collection_policy.effective(db, tenant_id, resource_type)
    return ok({"tenant_id": tenant_id, "resource_type": resource_type,
               "master_enabled": collect_switch.load(db).enabled(tenant_id, resource_type),
               "policy": policy.model_dump(mode="json"),
               "phase_status": collection_policy.phase_status(db, tenant_id, policy)})


@router.put("/{tenant_id}/{resource_type}", summary="保存租户 ECS 采集策略（不启用采集总开关）",
            response_model=ApiResponse[CollectionPolicySaveOut])
def put_policy(tenant_id: int, resource_type: str, body: CollectionPolicyUpdate, db: Session = Depends(get_db),
               scope: Scope = Depends(get_scope), audit: Auditor = Depends(get_auditor),
               cu: CurrentUser = Depends(require_perm("collect:manage"))):
    _check(db, scope, tenant_id, resource_type)
    result = collection_policy.save(db, tenant_id, resource_type, body, cu.display_name or cu.username,
                                    changes=body.model_dump(mode="json", exclude_unset=True))
    audit.log("collection_policy.update", target_type="collection_policy", target_id=f"{tenant_id}:{resource_type}",
              tenant_id=tenant_id, detail={"before": result["previous"], "after": result["policy"],
                                           "closed_alarms": result["closed_alarms"]})
    return ok({"tenant_id": tenant_id, "resource_type": resource_type,
               "master_enabled": collect_switch.load(db).enabled(tenant_id, resource_type),
               "policy": result["policy"], "closed_alarms": result["closed_alarms"],
               "phase_status": collection_policy.phase_status(db, tenant_id,
                   CollectionPolicy.model_validate(result["policy"]))})
