"""Durable execution API: authorization and database admission only, never cloud calls."""
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_auditor
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.response import ApiResponse, BizError, ErrorCode, Page, not_found, ok
from app.models import InspectionTask
from app.models.task_execution import TaskExecution
from app.schemas.task_execution import (
    ExecutionAdmissionOut,
    ExecutionOut,
    ExecutionStatus,
    ExecutionTargetsOut,
)
from app.services import execution_views

router = APIRouter(prefix="/system", tags=["任务执行"])


@router.get("/tasks/{code}/targets", summary="当前授权内的巡检目标与可执行原因",
            response_model=ApiResponse[ExecutionTargetsOut])
def task_targets(code: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                 _: CurrentUser = Depends(require_perm("task:view"))):
    task = db.get(InspectionTask, code)
    if task is None:
        raise not_found("巡检任务")
    return ok(execution_views.targets(db, scope, task))


@router.get("/executions", summary="按授权范围分页查询任务执行", response_model=ApiResponse[Page[ExecutionOut]])
def executions(db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
               _: CurrentUser = Depends(require_perm("task:view")), task_code: str | None = None,
               tenant_id: Annotated[int | None, Query(ge=1)] = None,
               project_ref: Annotated[int | None, Query(ge=1)] = None, status: ExecutionStatus | None = None,
               page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=100)] = 20):
    q = execution_views.query(scope)
    if tenant_id is not None:
        scope.ensure(tenant_id, "租户")
        q = q.where(TaskExecution.tenant_id == tenant_id)
    if project_ref is not None:
        q = q.where(TaskExecution.project_ref == project_ref)
    if task_code is not None:
        q = q.where(TaskExecution.task_code == task_code)
    if status is not None:
        q = q.where(TaskExecution.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = list(db.scalars(q.order_by(TaskExecution.created_at.desc(), TaskExecution.id.desc())
                          .offset((page - 1) * page_size).limit(page_size)))
    return ok({"items": execution_views.snapshots(db, rows), "total": total, "page": page, "page_size": page_size})


@router.get("/executions/{execution_id}", summary="查看任务执行进度和阶段历史", response_model=ApiResponse[ExecutionOut])
def execution(execution_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
              _: CurrentUser = Depends(require_perm("task:view"))):
    row = execution_views.owned(db, scope, execution_id)
    return ok(execution_views.snapshots(db, [row])[0])


@router.post("/executions/{execution_id}/cancel", summary="请求停止本地巡检（不停止云端作业）",
             response_model=ApiResponse[ExecutionOut])
def cancel_execution(execution_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                     audit: Auditor = Depends(get_auditor), user: CurrentUser = Depends(require_perm("task:run"))):
    from app.services import task_execution

    row = execution_views.owned(db, scope, execution_id)
    if row.task_code == "data_cleanup" and not scope.full:
        raise BizError(ErrorCode.FORBIDDEN, "数据清理为全局任务，需要全部租户的数据权限", 403)
    row = task_execution.request_cancel(db, row.id, user.display_name or user.username)
    audit.log("task.cancel", target_type="task_execution", target_id=row.id, tenant_id=row.tenant_id,
              detail={"task_code": row.task_code, "project_ref": row.project_ref, "local_only": True})
    return ok(execution_views.snapshots(db, [row])[0])


@router.post("/executions/{execution_id}/retry", status_code=202, summary="在原目标上创建新的排队执行",
             response_model=ApiResponse[ExecutionAdmissionOut])
def retry_execution(execution_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                    audit: Auditor = Depends(get_auditor), user: CurrentUser = Depends(require_perm("task:run"))):
    from app.services import task_execution

    row = execution_views.owned(db, scope, execution_id)
    if row.task_code == "data_cleanup" and not scope.full:
        raise BizError(ErrorCode.FORBIDDEN, "数据清理为全局任务，需要全部租户的数据权限", 403)
    if row.status not in execution_views.RETRYABLE:
        raise BizError(ErrorCode.CONFLICT, "仅失败、部分完成、超时、中断或已取消的执行可重试", 409)
    if row.project_ref is None and row.task_code != "data_cleanup":
        raise BizError(ErrorCode.CONFLICT, "原执行未记录项目，不能扩大范围重试，请重新选择目标", 409)
    refs = [row.project_ref] if row.project_ref is not None else None
    execution_views.ensure_projects(db, scope, refs)
    task = db.get(InspectionTask, row.task_code)
    if task is None:
        raise not_found("巡检任务")
    if not task.enabled:
        raise BizError(ErrorCode.CONFLICT, "巡检任务已禁用，请先启用任务", 409)
    result = task_execution.enqueue(db, row.task_code,
        tenant_ids=[row.tenant_id] if row.tenant_id is not None else None, project_refs=refs,
        trigger="MANUAL", requested_by=user.display_name or user.username, request_key=uuid4().hex, retry_of=row.id)
    data = execution_views.admission(db, scope, result)
    audit.log("task.enqueue", target_type="task", target_id=row.task_code, target_name=task.name,
              tenant_id=row.tenant_id, detail={"retry_of": row.id, "execution_ids": data["execution_ids"],
                                             "request_id": data["request_id"], "outcome": data["status"]})
    return ok(data, "重试已接受，等待后台执行")
