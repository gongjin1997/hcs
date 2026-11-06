"""作业专项报表：本地最新快照，只读，不触发采集或生成持久化报表。"""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import Scope, get_scope, require_perm
from app.core.response import ApiResponse, ok
from app.core.xlsx import filename_stamp, xlsx_response
from app.models.base import utcnow
from app.reports.job_excel import PANEL_TITLES, job_workbook
from app.schemas.job_report import JobPanel, JobReportOut, JobResourceType
from app.services.job_report_service import JobReportFilters, get_job_report

router = APIRouter(prefix="/job-reports", tags=["作业专项报表"])


def job_report_filters(
    keyword: str | None = Query(None, max_length=255),
    status: list[str] | None = Query(None, description="展示状态，多选"),
    status_raw: list[str] | None = Query(None, description="原始状态，多选"),
    resource_type: JobResourceType | None = None,
    engine: str | None = Query(None, max_length=128),
    job_type: str | None = Query(None, max_length=128),
    tenant_id: int | None = Query(None, ge=1),
    project_ref: int | None = Query(None, ge=1),
    lifecycle: Literal["ACTIVE", "DELETED", "ALL"] = "ACTIVE",
    has_alarms: bool | None = None,
) -> JobReportFilters:
    return JobReportFilters(keyword=keyword, status=status, status_raw=status_raw, resource_type=resource_type,
                            engine=engine, job_type=job_type, tenant_id=tenant_id, project_ref=project_ref,
                            lifecycle=lifecycle, has_alarms=has_alarms)


@router.get("/{panel}", response_model=ApiResponse[JobReportOut], summary="查询作业专项报表",
            dependencies=[Depends(require_perm("report:view"))])
def job_report(
    panel: JobPanel,
    filters: JobReportFilters = Depends(job_report_filters),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
) -> dict:
    return ok(get_job_report(db, scope, panel, filters, page=page, page_size=page_size))


@router.get("/{panel}/export", summary="导出作业报表 Excel（当前筛选全量）",
            dependencies=[Depends(require_perm("report:view"))])
def export_job_report(
    panel: JobPanel,
    filters: JobReportFilters = Depends(job_report_filters),
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
) -> Response:
    content = job_workbook(db, scope, panel, filters)
    return xlsx_response(content, f"{PANEL_TITLES[panel]}_{filename_stamp(utcnow())}.xlsx")
