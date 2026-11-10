"""持久执行队列：HTTP/调度只入队，执行器持有有限租约。"""
import logging
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.response import BizError, ErrorCode, not_found
from app.models import InspectionRun, InspectionTask, TaskExecution, TaskExecutionCancel
from app.models.base import utcnow

from . import cce_settings, collect_switch, collection_policy
from .catalog import CLEANUP_TASK_CODE
from .tenancy import Target, resolve_targets

log = logging.getLogger(__name__)
ACTIVE = ("QUEUED", "RUNNING")
RETRYABLE = ("FAILED", "PARTIAL", "TIMEOUT", "INTERRUPTED", "CANCELLED")
TERMINAL = ("SUCCESS", "PARTIAL", "FAILED", "TIMEOUT", "INTERRUPTED", "CANCELLED", "SKIPPED")
LEASE_SECONDS = 30
QUEUE_SECONDS = 1800


def get_owned(db: Session, execution_id: str) -> TaskExecution:
    row = db.get(TaskExecution, execution_id)
    if row is None:
        raise not_found("执行记录")
    return row


def scope_key(code: str, tenant_id: int | None, project_ref: int | None) -> str:
    return sha256(f"{code}:{tenant_id}:{project_ref}".encode()).hexdigest()


def target_enabled(db: Session, code: str, tenant_id: int | None, project_ref: int | None,
                   *, scheduled: bool = False) -> bool:
    if code == CLEANUP_TASK_CODE:
        return True
    if not collect_switch.load(db).task_enabled(tenant_id, code):
        return False
    if code == cce_settings.TASK_CODE and not cce_settings.status(db, tenant_id or 0)["effective_enabled"]:
        return False
    phase = collection_policy.TASK_PHASES.get(code)
    return phase is None or (collection_policy.stage_enabled(db, tenant_id, phase)
                            and (not scheduled or collection_policy.due(db, tenant_id, project_ref, phase)))


def enqueue(db: Session, task_code: str, tenant_ids: list[int] | None = None, *,
            project_refs: list[int] | None = None, trigger: str = "MANUAL", requested_by: str | None = None,
            request_key: str | None = None, retry_of: str | None = None, force: bool = False,
            inventory_only: bool = False) -> dict:
    # 同任务的准入串行化；SQLite 也必须先取得写锁再读 active_scope。
    db.execute(update(InspectionTask).where(InspectionTask.code == task_code).values(code=task_code))
    task = db.get(InspectionTask, task_code)
    if task is None:
        raise not_found("巡检任务")
    if not task.enabled and not force:
        raise BizError(ErrorCode.CONFLICT, "任务已停用，请先开启任务", 409)
    now = utcnow()
    rid = uuid4().hex
    key = request_key or rid
    if not key or len(key) > 128:
        raise BizError(ErrorCode.BAD_REQUEST, "请求标识长度须为 1–128 个字符")
    targets: list[Target | None] = [None] if task_code == CLEANUP_TASK_CODE else list(resolve_targets(db, tenant_ids))
    if task_code == CLEANUP_TASK_CODE and (tenant_ids is not None or project_refs is not None):
        raise BizError(ErrorCode.BAD_REQUEST, "数据清理为全局任务，不能指定租户或项目")
    if project_refs is not None:
        targets = [t for t in targets if t is not None and t.project_ref in project_refs]
    source = get_owned(db, retry_of) if retry_of else None
    if source is not None:
        if source.task_code != task_code or source.status not in RETRYABLE:
            raise BizError(ErrorCode.CONFLICT, "当前执行状态不支持重试", 409)
        targets = [t for t in targets if (t.tenant_id if t else None, t.project_ref if t else None)
                   == (source.tenant_id, source.project_ref)]
    result: dict = {"execution_ids": [], "runs": [], "skipped": [], "status": "QUEUED",
                    "deduplicated": False, "request_id": rid}
    for target in targets:
        tenant_id, project_ref = (target.tenant_id, target.project_ref) if target else (None, None)
        if inventory_only and not collection_policy.due(db, tenant_id, project_ref, "inventory"):
            continue
        skey = scope_key(task_code, tenant_id, project_ref)
        row = db.scalar(select(TaskExecution).where(TaskExecution.scope_key == skey,
            or_(TaskExecution.request_key == key, TaskExecution.active_scope == skey)).order_by(
                (TaskExecution.request_key == key).desc(), TaskExecution.created_at.desc()).limit(1))
        if row is not None:
            result["deduplicated"] = True
        elif not target_enabled(db, task_code, tenant_id, project_ref, scheduled=trigger == "SCHEDULE"):
            result["skipped"].append(f"{target.label if target else '全局'}：采集已关闭或尚未到期")
            continue
        else:
            params = dict(task.params or {})
            if inventory_only:
                params["_ledger_types"] = ["ecs_server"]
            timeout = params.get("timeout_sec", 300)
            timeout = max(15, min(3600, timeout if type(timeout) is int else 300))
            run = InspectionRun(task_code=task_code, tenant_id=tenant_id, project_ref=project_ref,
                                trigger_type=trigger, started_at=now, status="QUEUED", stats={})
            db.add(run)
            db.flush()
            row = TaskExecution(id=str(uuid4()), run_id=run.id, task_code=task_code, tenant_id=tenant_id,
                project_ref=project_ref, request_id=rid, request_key=key, scope_key=skey, active_scope=skey,
                trigger_type=trigger, requested_by=requested_by, status="QUEUED", created_at=now,
                queue_expires_at=now + timedelta(seconds=QUEUE_SECONDS), timeout_sec=timeout, params=params,
                target={"tenant_label": target.label, "project_label": target.project_name,
                        "project_id": target.project_id} if target else {},
                progress={"phase": "queued", "message": "等待执行器领取"},
                history=[{"phase": "queued", "at": now.isoformat(), "message": "已加入队列",
                          **({"retry_of": retry_of} if retry_of else {})}])
            db.add(row)
            db.flush()
        result["execution_ids"].append(row.id)
        result["runs"].append({"id": row.run_id, "run_id": row.run_id, "execution_id": row.id,
                              "task_code": row.task_code, "tenant_id": tenant_id, "project_ref": project_ref,
                              "status": row.status})
    if not result["execution_ids"]:
        result["status"] = "SKIPPED"
    db.commit()
    log.info("execution_enqueued", extra={"event": "execution_enqueued", "task": task_code,
        "execution_ids": result["execution_ids"], "request_id": rid, "deduplicated": result["deduplicated"],
        "target_count": len(result["execution_ids"]), "skipped_count": len(result["skipped"])})
    return result


def finish(db: Session, row: TaskExecution, status: str, message: str | None = None, *, code: str | None = None) -> None:
    """调用者须锁定 execution；终态不可覆盖。与运行记录在同一事务写入。"""
    if row.status not in ACTIVE:
        return
    now = utcnow()
    row.status, row.finished_at, row.active_scope, row.error_code = status, now, None, code
    row.lease_expires_at = None
    run = db.get(InspectionRun, row.run_id)
    if run is not None:
        run.status, run.finished_at = status, now
        if message:
            run.error_summary = message[:2000]
        if isinstance(row.progress.get("api_calls"), int):
            run.api_calls = max(run.api_calls or 0, row.progress["api_calls"])
    row.history = [*(row.history or [])[-99:], {"phase": "finished", "at": now.isoformat(),
                   "status": status, "message": message or status}]
    row.progress = {**(row.progress or {}), "phase": "finished", "message": message or status}
    if run is not None:
        from .platform_watch import defer_on_run

        defer_on_run(db, row.run_id)
    log.info("execution_finished", extra={"event": "execution_finished", "task_execution_id": row.id,
             "run_id": row.run_id, "task": row.task_code, "tenant_id": row.tenant_id,
             "project_ref": row.project_ref, "status": status, "error_code": code})


def request_cancel(db: Session, execution_id: str, operator: str | None) -> TaskExecution:
    row = get_owned(db, execution_id)
    if row.status not in ACTIVE:
        return row
    # 单独的请求表允许执行器持有提交栅栏时仍能接收取消；不会调用云作业停止接口。
    if db.get(TaskExecutionCancel, row.id) is None:
        try:
            with db.begin_nested():
                db.add(TaskExecutionCancel(execution_id=row.id, requested_by=operator or "system"))
                db.flush()
        except IntegrityError:
            pass
    db.commit()
    log.info("execution_cancel_requested", extra={"event": "execution_cancel_requested", "task_execution_id": row.id})
    return row


def claim_next(db: Session, worker_id: str) -> TaskExecution | None:
    now = utcnow()
    row = db.scalar(select(TaskExecution).where(TaskExecution.status == "QUEUED",
        TaskExecution.queue_expires_at > now).order_by(TaskExecution.created_at, TaskExecution.id)
        .with_for_update(skip_locked=True).limit(1))
    if row is None:
        db.rollback()
        return None
    token = uuid4().hex
    claimed = db.scalar(update(TaskExecution).where(TaskExecution.id == row.id, TaskExecution.status == "QUEUED")
        .values(status="RUNNING", owner_token=token, worker_id=worker_id, started_at=now, heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=LEASE_SECONDS), deadline_at=now + timedelta(seconds=row.timeout_sec))
        .returning(TaskExecution.id))
    if claimed is None:
        db.rollback()
        return None
    db.refresh(row)
    run = db.get(InspectionRun, row.run_id)
    if run:
        run.status, run.started_at = "RUNNING", now
    row.progress = {"phase": "starting", "message": "检查执行范围和配置"}
    row.history = [*(row.history or []), {"phase": "starting", "at": now.isoformat(), "message": "执行器已领取"}]
    db.commit()
    log.info("execution_claimed", extra={"event": "execution_claimed", "task_execution_id": row.id,
             "run_id": row.run_id, "worker_id": worker_id, "timeout_sec": row.timeout_sec})
    return row


def reap(db: Session, *, legacy_before: datetime | None = None) -> int:
    now = utcnow()
    rows = list(db.scalars(select(TaskExecution).where(TaskExecution.status.in_(ACTIVE), or_(
        (TaskExecution.status == "QUEUED") & (TaskExecution.queue_expires_at <= now), TaskExecution.lease_expires_at <= now,
        TaskExecution.deadline_at <= now,
        exists().where(TaskExecutionCancel.execution_id == TaskExecution.id)))
        .with_for_update(skip_locked=True).limit(200)))
    changed = 0
    for row in rows:
        if db.get(TaskExecutionCancel, row.id) is not None:
            # 活动子进程由所属 supervisor 先停止，再发布取消终态。
            if row.status == "RUNNING" and row.lease_expires_at and row.lease_expires_at > now:
                continue
            finish(db, row, "CANCELLED", "已取消本地巡检", code="LOCAL.CANCELLED")
        elif row.status == "RUNNING" and row.deadline_at and row.deadline_at <= now:
            finish(db, row, "TIMEOUT", "已达到本次巡检执行期限", code="LOCAL.DEADLINE")
        elif row.status == "RUNNING" and row.lease_expires_at and row.lease_expires_at <= now:
            finish(db, row, "INTERRUPTED", "执行器心跳中断；本次巡检已结束，可重新执行", code="LOCAL.LEASE_LOST")
        elif row.status == "QUEUED" and row.queue_expires_at <= now:
            finish(db, row, "TIMEOUT", "等待执行器超时，请检查调度服务", code="LOCAL.QUEUE_EXPIRED")
        else:
            continue
        changed += 1
    if legacy_before is not None:
        stale = list(db.scalars(select(InspectionRun).where(InspectionRun.status == "RUNNING",
            InspectionRun.started_at < legacy_before,
            ~exists().where(TaskExecution.run_id == InspectionRun.id)).with_for_update(skip_locked=True)))
        for run in stale:
            run.status, run.finished_at = "INTERRUPTED", now
            run.error_summary = "服务升级前的执行没有持久执行器记录，已结束；请重新执行"
            from .platform_watch import defer_on_run

            defer_on_run(db, run.id)
        changed += len(stale)
    db.commit()
    return changed
