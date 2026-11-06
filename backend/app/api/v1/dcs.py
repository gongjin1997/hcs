"""DCS 实例历史内存与 Excel 导出（只读、按租户授权）。"""
from datetime import datetime

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import Scope, get_scope, require_perm
from app.core.response import ApiResponse, ok
from app.core.xlsx import filename_stamp, xlsx_response
from app.reports.dcs_excel import export_memory, export_metrics
from app.schemas.dcs import DcsHistoryOut, DcsInstanceOut, DcsInstancesOut, DcsMetricsHistoryOut
from app.services.dcs_history_service import MemoryQuery, get_instance, list_instances, memory_history
from app.services.dcs_monitoring import metrics_history

router = APIRouter(prefix="/dcs", tags=["DCS 内存监控"], dependencies=[Depends(require_perm("report:view"))])


def memory_query(resource_ids: list[int] = Query(min_length=1, max_length=20),
                 start_at: datetime = Query(), end_at: datetime = Query(),
                 interval_minutes: int = Query(5, json_schema_extra={"enum": [5, 10, 15, 30, 60]})) -> MemoryQuery:
    return MemoryQuery.create(resource_ids, start_at, end_at, interval_minutes)


@router.get("/instances", response_model=ApiResponse[DcsInstancesOut], summary="DCS 实例列表")
def instances(keyword: str | None = Query(None, max_length=200), tenant_id: int | None = Query(None, ge=1),
              project_ref: int | None = Query(None, ge=1), page: int = Query(1, ge=1),
              page_size: int = Query(100, ge=1, le=100), db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(list_instances(db, scope, keyword=keyword, tenant_id=tenant_id, project_ref=project_ref,
                             page=page, page_size=page_size))


@router.get("/instances/{resource_ref}", response_model=ApiResponse[DcsInstanceOut], summary="DCS 本地单实例详情")
def instance(resource_ref: int = Path(ge=1), db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(get_instance(db, scope, resource_ref))


@router.get("/history", response_model=ApiResponse[DcsHistoryOut], summary="DCS 内存历史（五分钟原始点聚合）")
def history(query: MemoryQuery = Depends(memory_query), db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(memory_history(db, scope, query))


@router.get("/export", summary="按同一筛选导出完整 DCS 内存指标 Excel")
def export(query: MemoryQuery = Depends(memory_query), db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    data = memory_history(db, scope, query)
    content = export_memory(data)
    start, end = filename_stamp(query.start_at, "%Y%m%d"), filename_stamp(query.end_at, "%Y%m%d")
    return xlsx_response(content, f"DCS内存指标_{start}_{end}.xlsx")


@router.get("/metrics/history", response_model=ApiResponse[DcsMetricsHistoryOut], summary="DCS V2 原生统计本地真实采样趋势")
def native_history(query: MemoryQuery = Depends(memory_query), db: Session = Depends(get_db),
                   scope: Scope = Depends(get_scope)):
    return ok(metrics_history(db, scope, query))


@router.get("/metrics/export", summary="按同一筛选导出完整 DCS V2 原生统计 Excel")
def native_export(query: MemoryQuery = Depends(memory_query), db: Session = Depends(get_db),
                  scope: Scope = Depends(get_scope)):
    start, end = filename_stamp(query.start_at, "%Y%m%d"), filename_stamp(query.end_at, "%Y%m%d")
    return xlsx_response(export_metrics(metrics_history(db, scope, query)), f"DCS原生指标_{start}_{end}.xlsx")
