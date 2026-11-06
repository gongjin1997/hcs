"""ECS 使用指标、单机趋势和同条件 XLSX 导出。"""
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import Scope, get_scope, require_perm
from app.core.response import ApiResponse, ok
from app.core.xlsx import filename_stamp, xlsx_response
from app.reports.ecs_excel import export_metrics
from app.schemas.ecs import EcsHistoryOut, EcsInstancesOut
from app.services.ecs_monitoring import HistoryQuery, history, list_instances

router = APIRouter(prefix="/ecs", tags=["ECS 监控"])
VIEW = Depends(require_perm("ledger:view"))


def history_query(start_at: datetime = Query(), end_at: datetime = Query(), interval_sec: int = Query(300),
                  metrics: list[str] | None = Query(None, max_length=3)) -> HistoryQuery:
    return HistoryQuery.create(start_at, end_at, interval_sec, metrics)


@router.get("/instances", summary="ECS 实例及真实采样时效", response_model=ApiResponse[EcsInstancesOut], dependencies=[VIEW])
def instances(tenant_id: int | None = Query(None, ge=1), project_ref: int | None = Query(None, ge=1),
              keyword: str | None = Query(None, max_length=200), monitored: bool | None = None,
              critical: bool | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
              db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(list_instances(db, scope, tenant_id=tenant_id, project_ref=project_ref, keyword=keyword,
              monitored=monitored, critical=critical, page=page, page_size=page_size))


@router.get("/instances/{resource_ref}/history", summary="ECS 单机 CPU、内存和磁盘趋势",
            response_model=ApiResponse[EcsHistoryOut], dependencies=[VIEW])
def instance_history(resource_ref: int, query: HistoryQuery = Depends(history_query),
                     db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(history(db, scope, resource_ref, query))


@router.get("/instances/{resource_ref}/export", summary="按同一筛选导出 ECS 趋势 XLSX",
            dependencies=[VIEW, Depends(require_perm("ledger:export"))])
def instance_export(resource_ref: int, query: HistoryQuery = Depends(history_query),
                    db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    result = history(db, scope, resource_ref, query)
    start, end = filename_stamp(query.start_at, "%Y%m%d"), filename_stamp(query.end_at, "%Y%m%d")
    return xlsx_response(export_metrics(result), f"ECS指标_{resource_ref}_{start}_{end}.xlsx")
