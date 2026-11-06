"""GaussDB 原生诊断，只读实例、磁盘、慢 SQL、会话与巡检报告。"""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, forbidden, get_scope, require_perm
from app.core.response import ApiResponse, BizError, ErrorCode, ok
from app.core.xlsx import xlsx_response
from app.reports.gaussdb_excel import export_native
from app.schemas.gaussdb import (
    GaussDiskHistoryOut,
    GaussInstance,
    GaussInstancesOut,
    GaussNativeRows,
    GaussOverviewOut,
    GaussProjectOption,
    GaussSummaryOut,
)
from app.services import gaussdb_monitoring as service
from app.services.gaussdb_history import disk_history

router = APIRouter(prefix="/gaussdb", tags=["GaussDB 原生诊断"], dependencies=[Depends(require_perm("ledger:view"))])
DIAGNOSE = Depends(require_perm("task:run"))


def slow_query(start_at: datetime = Query(), end_at: datetime = Query(),
               node_ids: list[str] = Query(min_length=1, max_length=20), threshold: int = Query(1, ge=1, le=99),
               sql_id: str | None = Query(None, min_length=1, max_length=255)) -> service.SlowQuery:
    return service.SlowQuery.create(start_at, end_at, node_ids, threshold, sql_id)


@router.get("/instances", response_model=ApiResponse[GaussInstancesOut],
            summary="GaussDB 实例列表（本地台账与最近一次巡检；异常在前）")
def instances(keyword: str | None = Query(None, max_length=200), tenant_id: int | None = Query(None, ge=1),
              project_ref: int | None = Query(None, ge=1),
              status_group: Literal["abnormal", "transitional", "stopped", "normal"] | None = Query(None),
              page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
              db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(service.list_instances(db, scope, keyword=keyword, tenant_id=tenant_id, project_ref=project_ref,
                                      status_group=status_group, page=page, page_size=page_size))


@router.get("/summary", response_model=ApiResponse[GaussSummaryOut],
            summary="GaussDB 概况：状态分组、磁盘高水位、组件宕机（本地）")
def instances_summary(tenant_id: int | None = Query(None, ge=1), project_ref: int | None = Query(None, ge=1),
                      db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(service.summary(db, scope, tenant_id=tenant_id, project_ref=project_ref))


@router.get("/projects", response_model=ApiResponse[list[GaussProjectOption]], summary="GaussDB 可见台账项目选项（不请求云）")
def projects(db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(service.list_projects(db, scope))


@router.get("/instances/{resource_ref}", response_model=ApiResponse[GaussInstance],
            summary="GaussDB 单实例本地台账（不请求云）")
def instance(resource_ref: int, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(service.get_instance(db, scope, resource_ref))


@router.get("/instances/{resource_ref}/overview", response_model=ApiResponse[GaussOverviewOut],
            summary="查询原生磁盘容量与节点组件", dependencies=[DIAGNOSE])
def overview(resource_ref: int, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(service.overview(db, scope, resource_ref))


@router.get("/instances/{resource_ref}/disk-history", response_model=ApiResponse[GaussDiskHistoryOut],
            summary="读取本地定时采样的原生存储历史（不请求云）")
def storage_history(resource_ref: int, start_at: datetime = Query(), end_at: datetime = Query(),
                    period_sec: int = Query(300, ge=60, le=86400), db: Session = Depends(get_db),
                    scope: Scope = Depends(get_scope)):
    return ok(disk_history(db, scope, resource_ref, start_at=start_at, end_at=end_at, period_sec=period_sec))


@router.get("/instances/{resource_ref}/slow-nodes", response_model=ApiResponse[GaussNativeRows],
            summary="查询支持慢 SQL 的原生 CN/DN 节点", dependencies=[DIAGNOSE])
def slow_nodes(resource_ref: int, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(service.slow_nodes(db, scope, resource_ref))


@router.get("/instances/{resource_ref}/slow-sql", response_model=ApiResponse[GaussNativeRows],
            summary="按节点和时间查询慢 SQL（耗时单位微秒）", dependencies=[DIAGNOSE])
def slow_sql(resource_ref: int, query: service.SlowQuery = Depends(slow_query),
             db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(service.slow_sql(db, scope, resource_ref, query))


@router.get("/instances/{resource_ref}/slow-sql/detail", response_model=ApiResponse[GaussNativeRows],
            summary="查询慢 SQL 模板的执行详情", dependencies=[DIAGNOSE])
def slow_detail(resource_ref: int, query: service.SlowQuery = Depends(slow_query),
                db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(service.slow_sql(db, scope, resource_ref, query, detail=True))


@router.get("/instances/{resource_ref}/sessions", response_model=ApiResponse[GaussNativeRows],
            summary="只读查询原生实时会话，不提供终止操作", dependencies=[DIAGNOSE])
def sessions(resource_ref: int, node_id: str = Query(min_length=1, max_length=255),
             component_id: str = Query(min_length=1, max_length=255), limit: int = Query(100, ge=1, le=200),
             database_name: str | None = Query(None, max_length=255), user_name: str | None = Query(None, max_length=255),
             client_ip: str | None = Query(None, max_length=255), state: str | None = Query(None, max_length=255),
             wait: Literal["t", "f"] | None = Query(None), db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    filters = {key: value for key, value in {"database_name": database_name, "user_name": user_name,
                "client_ip": client_ip, "state": state, "wait": wait}.items() if value is not None}
    return ok(service.sessions(db, scope, resource_ref, node_id, component_id, limit=limit, filters=filters))


@router.get("/instances/{resource_ref}/inspections", response_model=ApiResponse[GaussNativeRows],
            summary="读取本实例已完成的原生巡检记录", dependencies=[DIAGNOSE])
def inspections(resource_ref: int, start_at: datetime | None = Query(None), end_at: datetime | None = Query(None),
                limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1000),
                db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(service.inspections(db, scope, resource_ref, start_at=start_at, end_at=end_at, limit=limit, offset=offset))


@router.get("/instances/{resource_ref}/inspections/{record_id}", response_model=ApiResponse[GaussNativeRows],
            summary="读取原生巡检项和原始时间点，校验记录实例归属", dependencies=[DIAGNOSE])
def inspection_detail(resource_ref: int, record_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return ok(service.inspection_detail(db, scope, resource_ref, record_id))


@router.get("/instances/{resource_ref}/export", summary="导出同条件 GaussDB 原生查询 XLSX",
            dependencies=[Depends(require_perm("ledger:export"))])
def export(resource_ref: int, panel: Literal["overview", "disk-history", "slow-sql", "slow-sql-detail", "sessions",
                                           "inspections", "inspection-detail"],
           start_at: datetime | None = Query(None), end_at: datetime | None = Query(None),
           node_ids: list[str] | None = Query(None, max_length=20), threshold: int = Query(1, ge=1, le=99),
           sql_id: str | None = Query(None, max_length=255), node_id: str | None = Query(None, max_length=255),
           component_id: str | None = Query(None, max_length=255), record_id: str | None = Query(None, max_length=255),
           limit: int | None = Query(None, ge=1, le=200), offset: int = Query(0, ge=0, le=1000),
           period_sec: int = Query(300, ge=60, le=86400),
           database_name: str | None = Query(None, max_length=255), user_name: str | None = Query(None, max_length=255),
           client_ip: str | None = Query(None, max_length=255), state: str | None = Query(None, max_length=255),
           wait: Literal["t", "f"] | None = Query(None), db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
           user: CurrentUser = Depends(require_perm("ledger:view"))):
    if panel != "disk-history" and not user.can("task:run"):
        raise forbidden(("task:run",))
    target = service._target(db, scope, resource_ref)
    conditions = {}
    result: GaussNativeRows | GaussOverviewOut | GaussDiskHistoryOut
    if panel == "disk-history":
        if start_at is None or end_at is None:
            raise BizError(ErrorCode.BAD_REQUEST, "导出存储历史须提供起止时间")
        result = disk_history(db, scope, resource_ref, start_at=start_at, end_at=end_at, period_sec=period_sec)
        conditions = {"开始时间": result.start_at, "结束时间": result.end_at, "分桶间隔（秒）": period_sec}
    elif panel == "overview":
        result = service.overview(db, scope, resource_ref)
    elif panel in ("slow-sql", "slow-sql-detail"):
        if start_at is None or end_at is None or not node_ids:
            raise BizError(ErrorCode.BAD_REQUEST, "导出慢 SQL 须提供时间范围和节点")
        query = service.SlowQuery.create(start_at, end_at, node_ids, threshold, sql_id)
        result = service.slow_sql(db, scope, resource_ref, query, detail=panel == "slow-sql-detail")
        conditions = {"开始时间": query.start_at, "结束时间": query.end_at, "节点": list(query.node_ids), "SQL ID": sql_id,
                      **({"阈值（秒）": threshold} if panel == "slow-sql" else {})}
    elif panel == "sessions":
        if not node_id or not component_id:
            raise BizError(ErrorCode.BAD_REQUEST, "导出会话须提供节点和组件")
        filters = {key: value for key, value in {"database_name": database_name, "user_name": user_name,
                   "client_ip": client_ip, "state": state, "wait": wait}.items() if value is not None}
        result = service.sessions(db, scope, resource_ref, node_id, component_id, limit=limit or 100, filters=filters)
        conditions = {"节点": node_id, "组件": component_id, "查询上限": limit or 100, **filters}
    elif panel == "inspection-detail":
        if not record_id:
            raise BizError(ErrorCode.BAD_REQUEST, "导出巡检指标须选择巡检记录")
        result = service.inspection_detail(db, scope, resource_ref, record_id)
        conditions = {"巡检记录": record_id, "巡检开始时间": result.start_at, "巡检结束时间": result.end_at}
    else:
        result = service.inspections(db, scope, resource_ref, start_at=start_at, end_at=end_at,
                                     limit=limit or 50, offset=offset)
        conditions = {"开始时间": start_at, "结束时间": end_at, "查询条数": limit or 50, "偏移": offset}
    content = export_native(service.instance_out(target), panel, result, conditions)
    return xlsx_response(content, f"GaussDB_{panel}_{resource_ref}.xlsx")
