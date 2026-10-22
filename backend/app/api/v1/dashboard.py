from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import Scope, get_scope, require_perm
from app.core.response import ok
from app.services import dashboard_service

router = APIRouter(prefix="/dashboard", tags=["总览"])


@router.get("/summary", summary="总览 / 大屏汇总数据（按租户范围）", dependencies=[Depends(require_perm("dashboard:view"))])
def summary(db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(dashboard_service.summary(db, tenant_ids=scope.tenant_ids))
