"""统一资源监控入口的本地只读覆盖信息。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import Scope, get_scope, require_perm
from app.core.response import ApiResponse, ok
from app.schemas.monitoring import MonitoringCoverageOut
from app.services.monitoring_overview import coverage

router = APIRouter(prefix="/monitoring", tags=["监控覆盖"],
                   dependencies=[Depends(require_perm("ledger:view", "report:view", any_of=True))])


@router.get("/coverage", response_model=ApiResponse[MonitoringCoverageOut],
            summary="当前授权租户的 ECS、DCS、GaussDB 本地监控覆盖（不请求云）")
def get_coverage(tenant_id: int | None = Query(None, ge=1), db: Session = Depends(get_db),
                 scope: Scope = Depends(get_scope)):
    return ok(coverage(db, scope, tenant_id=tenant_id))
