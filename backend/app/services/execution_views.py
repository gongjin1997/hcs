"""Scope-aware, read-only execution projections, shared by task and execution APIs."""
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.collectors.mrs_params import mrs_parameter_schema, validate_mrs_params
from app.core.deps import Scope
from app.core.logging import sanitize_log, sanitize_text
from app.core.response import BizError, ErrorCode, not_found
from app.hcs import HcsError
from app.models import HcsAccount, HcsProject, InspectionRun, InspectionTask, SysSetting
from app.models.base import utcnow
from app.models.task_execution import TaskExecution, TaskExecutionCancel
from app.schemas.task_execution import ExecutionOptionsOut, ExecutionOut, MRSExecutionOptionsOut
from app.services import cce_settings, collect_switch, collection_policy, platform_watch

ACTIVE = ("QUEUED", "RUNNING")
RETRYABLE = ("FAILED", "PARTIAL", "TIMEOUT", "INTERRUPTED", "CANCELLED")
DEFAULT_TIMEOUT_SEC = 300
PHASE_LABELS = {
    "QUEUED": "排队等待", "STARTING": "准备执行", "RUNNING": "正在巡检", "AUTH": "认证",
    "COLLECT": "采集", "COLLECTING": "采集资源", "EVALUATE": "规则判定", "EVALUATING": "规则评估",
    "PERSIST": "保存结果", "PERSISTING": "保存结果", "SUCCESS": "执行成功", "PARTIAL": "部分完成",
    "FAILED": "执行失败", "TIMEOUT": "执行超时", "INTERRUPTED": "执行中断", "CANCELLED": "已取消",
    "SKIPPED": "已跳过",
    "CLAIMED": "执行器已领取", "PREPARING": "准备执行", "FINISHED": "执行结束",
    "MRS_DISCOVERY": "发现 MRS 集群", "MRS_LIST": "查询 MRS 作业", "MRS_DETAILS": "读取 MRS 作业详情",
    "SAVING": "保存巡检结果", "INVENTORY_FETCH": "同步资源台账", "MRS_CLUSTERS": "发现 MRS 集群",
    "MRS_EXECUTIONS_PAGE": "读取 MRS 作业分页", "MRS_CLUSTER_EXECUTIONS": "逐集群巡检 MRS 作业",
    "MRS_EXECUTIONS_READ": "汇总 MRS 作业", "MRS_EXECUTIONS_PROCESS": "检查 MRS 作业状态",
    "MRS_COLLECTION_COMPLETE": "MRS 采集完成", "DATAARTS_MRS_WORKSPACE": "读取 DataArts 工作空间",
    "DATAARTS_JOB_LIST": "读取 DataArts 作业列表", "DATAARTS_REALTIME_STATUS": "查询实时作业状态",
    "DATAARTS_BATCH_INSTANCES": "查询批作业实例", "DATAARTS_DEFINITIONS": "读取作业定义",
    "DATAARTS_MRS_INSTANCES_PAGE": "读取 DataArts 实例分页", "DATAARTS_MRS_DETAILS": "读取 DataArts 实例详情",
    "DATAARTS_MRS_NODES_PROCESS": "检查 DataArts MRS 节点", "DATAARTS_MRS_COMPLETE": "DataArts MRS 采集完成",
}
PROGRESS_FIELDS = {"phase", "phase_label", "current", "total", "unit", "message", "at", "timestamp", "retry_of",
                   "api_calls", "resource_total", "abnormal_total", "status", "cluster_id", "query"}


def query(scope: Scope):
    return scope.apply(select(TaskExecution), TaskExecution.tenant_id, include_global=True)


def owned(db: Session, scope: Scope, execution_id: str) -> TaskExecution:
    row = db.scalar(query(scope).where(TaskExecution.id == execution_id))
    if row is None:
        raise not_found("任务执行")
    scope.ensure(row.tenant_id, "任务执行")
    return row


def ensure_projects(db: Session, scope: Scope, project_refs: list[int] | None) -> None:
    if project_refs is None:
        return
    if not project_refs:
        raise BizError(ErrorCode.BAD_REQUEST, "请至少选择一个项目")
    rows = db.execute(scope.apply(select(HcsProject.id, HcsProject.account_id).where(
        HcsProject.id.in_(project_refs)), HcsProject.account_id)).all()
    if {row.id for row in rows} != set(project_refs):
        raise not_found("巡检目标")
    for row in rows:
        scope.ensure(row.account_id, "巡检目标")


def parameter_view(task: InspectionTask) -> dict:
    """Only explicitly editable parameters cross the API boundary."""
    raw = task.params or {}
    timeout = raw.get("timeout_sec", DEFAULT_TIMEOUT_SEC)
    timeout = max(15, min(3600, timeout if type(timeout) is int else DEFAULT_TIMEOUT_SEC))
    params = {"timeout_sec": timeout}
    properties: dict[str, Any] = {"timeout_sec": {
        "type": "integer", "minimum": 15, "maximum": 3600, "default": DEFAULT_TIMEOUT_SEC, "title": "执行超时（秒）",
    }}
    if task.code == "dgc_batch_instance":
        minimum = raw.get("p90_min_samples", 5)
        params["p90_min_samples"] = minimum if type(minimum) is int and 1 <= minimum <= 10000 else 5
        properties["p90_min_samples"] = {"type": "integer", "minimum": 1, "maximum": 10000, "default": 5}
    if task.code == "mrs_job_status":
        mrs_schema = mrs_parameter_schema(task.code)
        fields = mrs_schema["properties"]
        selected = {k: v for k, v in raw.items() if k in fields}
        try:
            effective = validate_mrs_params(task.code, selected)
        except HcsError:
            effective = validate_mrs_params(task.code, {})
        params.update(effective)
        properties.update(fields)
    schema = {"type": "object", "additionalProperties": False, "properties": properties}
    return {"params": params, "effective_params": params, "parameter_schema": schema}


def _progress(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    # Reject nested arbitrary data even when stored underneath a public field name.
    out: dict[str, Any] = {}
    for key, entry in value.items():
        if key not in PROGRESS_FIELDS:
            continue
        if key in {"current", "total", "api_calls", "resource_total", "abnormal_total"}:
            if entry is None or type(entry) is int and entry >= 0:
                out[key] = entry
        elif entry is None or isinstance(entry, str):
            out[key] = sanitize_text(entry)[:1000] if isinstance(entry, str) else None
    return out


def snapshots(db: Session, executions: Sequence[TaskExecution], *, now: datetime | None = None) -> list[dict]:
    """Load run, name and cancellation information in constant query count."""
    if not executions:
        return []
    now = now or utcnow()
    runs = {r.id: r for r in db.scalars(select(InspectionRun).where(
        InspectionRun.id.in_([e.run_id for e in executions])))}
    tasks = {t.code: t for t in db.scalars(select(InspectionTask).where(
        InspectionTask.code.in_({e.task_code for e in executions})))}
    tenants: dict[int | None, str] = dict(db.execute(select(
        HcsAccount.id, func.coalesce(func.nullif(HcsAccount.short_name, ""), HcsAccount.name))
                             .where(HcsAccount.id.in_({e.tenant_id for e in executions if e.tenant_id is not None}))).all())
    projects: dict[int | None, str] = dict(db.execute(select(HcsProject.id, func.coalesce(
        func.nullif(HcsProject.project_name, ""), HcsProject.project_id)).where(
            HcsProject.id.in_({e.project_ref for e in executions if e.project_ref is not None}))).all())
    cancelled = set(db.scalars(select(TaskExecutionCancel.execution_id).where(
        TaskExecutionCancel.execution_id.in_([e.id for e in executions]))))
    out = []
    for execution in executions:
        run = runs.get(execution.run_id)
        task = tasks.get(execution.task_code)
        progress = _progress(execution.progress)
        phase = progress.get("phase") or execution.status
        phase = phase if isinstance(phase, str) else execution.status
        phase_label = progress.get("phase_label") or PHASE_LABELS.get(phase.upper(), phase)
        history = []
        for entry in (execution.history or [])[-100:]:
            if not isinstance(entry, dict):
                continue
            item_history = _progress(entry)
            history_phase = item_history.get("phase")
            if isinstance(history_phase, str):
                item_history.setdefault("phase_label", PHASE_LABELS.get(history_phase.upper(), history_phase))
            history.append(item_history)
        elapsed = max(0.0, ((execution.finished_at or now) - execution.started_at).total_seconds()) \
            if execution.started_at is not None else 0.0
        # Deleted target names may survive only as explicitly named display fields.
        target = execution.target if isinstance(execution.target, dict) else {}
        tenant_name = tenants.get(execution.tenant_id) or target.get("tenant_label") or target.get("tenant_name")
        project_label = projects.get(execution.project_ref) or target.get("project_label") or target.get("project_name")
        if not isinstance(tenant_name, str):
            tenant_name = f"租户#{execution.tenant_id}" if execution.tenant_id is not None else None
        if not isinstance(project_label, str):
            project_label = f"项目#{execution.project_ref}" if execution.project_ref is not None else \
                "全局内部任务" if execution.task_code == "data_cleanup" and execution.tenant_id is None \
                else "历史未知项目（未记录）"
        options = _execution_options(execution)
        item: dict[str, Any] = {"id": execution.id, "execution_id": execution.id, "run_id": execution.run_id,
                "task_code": execution.task_code, "task_name": task.name if task else execution.task_code,
                "service": task.service if task else "", "tenant_id": execution.tenant_id,
                "tenant_name": sanitize_text(tenant_name) if tenant_name else None,
                "project_ref": execution.project_ref, "project_label": sanitize_text(project_label),
                "request_id": execution.request_id, "status": execution.status, "trigger_type": execution.trigger_type,
                "phase": phase, "phase_label": phase_label, "current": progress.get("current"),
                "total": progress.get("total"), "unit": progress.get("unit"), "message": progress.get("message"),
                "created_at": execution.created_at, "queued_at": execution.created_at,
                "started_at": execution.started_at, "finished_at": execution.finished_at,
                "heartbeat_at": execution.heartbeat_at, "deadline_at": execution.deadline_at,
                "elapsed_sec": round(elapsed, 3), "timeout_sec": options["timeout_sec"], "options": options,
                "cancel_requested": execution.id in cancelled, "api_calls": run.api_calls if run else 0,
                "resource_total": run.resource_total if run else 0, "abnormal_total": run.abnormal_total if run else 0,
                "error_summary": sanitize_text(run.error_summary) if run and run.error_summary else None,
                "progress": progress, "history": history}
        if execution.status in ACTIVE:
            for field in ("api_calls", "resource_total", "abnormal_total"):
                reported = progress.get(field)
                if type(reported) is int:
                    item[field] = max(item[field], reported)
        out.append(ExecutionOut.model_validate(item).model_dump())
    return out


def _execution_options(execution: TaskExecution) -> dict:
    """Project only the public option schema from the immutable enqueue-time parameter copy."""
    raw = execution.params if isinstance(execution.params, dict) else {}
    timeout = execution.timeout_sec if type(execution.timeout_sec) is int and 15 <= execution.timeout_sec <= 3600 \
        else DEFAULT_TIMEOUT_SEC
    p90 = raw.get("p90_min_samples")
    p90 = p90 if execution.task_code == "dgc_batch_instance" and type(p90) is int and 1 <= p90 <= 10000 else None
    options: dict[str, Any] = {"timeout_sec": timeout}
    if p90 is not None:
        options["p90_min_samples"] = p90
    if execution.task_code == "mrs_job_status":
        schema = mrs_parameter_schema(execution.task_code)["properties"]
        selected = {key: raw[key] for key in schema if key in raw}
        try:
            effective = validate_mrs_params(execution.task_code, selected, strict=False)
        except HcsError:
            effective = validate_mrs_params(execution.task_code, {}, strict=False)
        options["mrs"] = MRSExecutionOptionsOut.model_validate(effective).model_dump(exclude_unset=True)
    return ExecutionOptionsOut.model_validate(options).model_dump(exclude_none=True)


def admission(db: Session, scope: Scope, result: dict) -> dict:
    ids = result.get("execution_ids", [])
    rows = list(db.scalars(query(scope).where(TaskExecution.id.in_(ids)).order_by(TaskExecution.created_at, TaskExecution.id)))
    items = snapshots(db, rows)
    skipped = [sanitize_text(value) for value in result.get("skipped", []) if isinstance(value, str)]
    if not items:
        raise BizError(ErrorCode.CONFLICT, "当前授权范围内没有可执行目标（采集已关闭、租户项目已禁用或尚未配置）", 409,
                       {"skipped": skipped})
    return {"execution_ids": [item["id"] for item in items], "runs": items,
            "skipped": skipped, "status": platform_watch.run_outcome(rows, skipped=bool(skipped)),
            "deduplicated": bool(result.get("deduplicated")),
            "request_id": result["request_id"]}


def task_execution_data(db: Session, scope: Scope, runs: Sequence[InspectionRun], *, project_ref: int | None = None,
                        unknown_project: bool = False) -> tuple[dict, dict, dict]:
    q = query(scope)
    if project_ref is not None:
        q = q.where(TaskExecution.project_ref == project_ref)
    if unknown_project:
        q = q.where(TaskExecution.project_ref.is_(None), TaskExecution.task_code != "data_cleanup")
    ranked = q.with_only_columns(TaskExecution.id, func.row_number().over(
        partition_by=TaskExecution.task_code,
        order_by=(TaskExecution.created_at.desc(), TaskExecution.id.desc())).label("position")).subquery()
    latest = list(db.scalars(select(TaskExecution).join(ranked, TaskExecution.id == ranked.c.id).where(ranked.c.position == 1)))
    related = list(db.scalars(q.where(TaskExecution.run_id.in_([r.id for r in runs])))) if runs else []
    combined = {e.id: e for e in [*latest, *related]}
    data = {item["id"]: item for item in snapshots(db, list(combined.values()))}
    counts = db.execute(q.with_only_columns(TaskExecution.task_code, TaskExecution.status, func.count())
                        .where(TaskExecution.status.in_(ACTIVE)).group_by(TaskExecution.task_code, TaskExecution.status)).all()
    totals: dict[str, dict] = {}
    for code, status, count in counts:
        item = totals.setdefault(code, {"active_execution_count": 0, "queued_count": 0})
        item["active_execution_count"] += count
        if status == "QUEUED":
            item["queued_count"] += count
    return ({e.task_code: data[e.id] for e in latest}, {e.run_id: data[e.id] for e in related}, totals)


class TargetAvailability:
    """Batch the local enablement sources once, including when listing every task."""

    def __init__(self, db: Session, scope: Scope, definitions: Sequence[InspectionTask]):
        self.definitions = {task.code: task for task in definitions}
        q = scope.apply(select(HcsAccount, HcsProject).join(
            HcsProject, HcsProject.account_id == HcsAccount.id), HcsAccount.id)
        self.rows = db.execute(q.order_by(HcsAccount.id, HcsProject.id)).all()
        keys = [key for tenant_id in {t.id for t, _ in self.rows}
                for key in (collection_policy.policy_key(tenant_id), cce_settings.key(tenant_id))]
        self.settings: dict[str, dict] = dict(db.execute(select(
            SysSetting.key, SysSetting.value).where(SysSetting.key.in_(keys))).all())
        self.switches = collect_switch.load(db)

    def gate(self, tenant_id: int, task: InspectionTask) -> str | None:
        if not self.switches.task_enabled(tenant_id, task.code):
            return "采集开关已关闭"
        phase = collection_policy.TASK_PHASES.get(task.code)
        if phase is not None:
            policy = self.settings.get(collection_policy.policy_key(tenant_id), {})
            enabled = policy.get(phase, {}).get("enabled", phase != "quota")
            if not self.switches.enabled(tenant_id, "ecs_server") or not enabled:
                return "采集阶段已关闭"
        if task.code == cce_settings.TASK_CODE:
            cfg = self.settings.get(cce_settings.key(tenant_id), {})
            if not cfg.get("enabled", False) or not self.switches.enabled(tenant_id, "cce_cluster"):
                return "容器事件采集已关闭"
        return None

    def skipped(self, task: InspectionTask) -> list[str]:
        if task.code == "data_cleanup":
            return []
        return list(dict.fromkeys(t.short_name or t.name for t, p in self.rows
                                 if t.enabled and p.enabled and self.gate(t.id, task) is not None))


def targets(db: Session, scope: Scope, task: InspectionTask) -> dict:
    from app.collectors.registry import TASKS

    availability = TargetAvailability(db, scope, [task])
    active = {(e.tenant_id, e.project_ref): e.id for e in db.scalars(query(scope).where(
        TaskExecution.task_code == task.code, TaskExecution.status.in_(ACTIVE)))}
    out = []
    for tenant, project in availability.rows:
        reason = None
        if not task.enabled:
            reason = "任务已禁用"
        elif task.code not in TASKS and task.code != "ledger_sync":
            reason = "任务没有对应的采集器"
        elif not tenant.enabled:
            reason = "租户已禁用"
        elif not project.enabled:
            reason = "项目已禁用"
        else:
            reason = availability.gate(tenant.id, task)
        execution_id = active.get((tenant.id, project.id))
        if reason is None and execution_id is not None:
            reason = "已有排队或执行中的巡检"
        out.append({"tenant_id": tenant.id, "tenant_name": tenant.short_name or tenant.name,
                    "project_ref": project.id, "project_label": project.project_name or project.project_id,
                    "enabled": tenant.enabled and project.enabled, "executable": reason is None,
                    "reason": reason, "active_execution_id": execution_id})
    if task.code == "data_cleanup":
        out = []
        if scope.full:
            execution_id = active.get((None, None))
            reason = "任务已禁用" if not task.enabled else "已有排队或执行中的巡检" if execution_id else None
            out.append({"tenant_id": None, "tenant_name": None, "project_ref": None,
                        "project_label": "全局内部任务", "enabled": True, "executable": reason is None,
                        "reason": reason, "active_execution_id": execution_id})
    return {"task_code": task.code, "task_enabled": task.enabled, "targets": out}


def safe_stats(value: dict) -> dict:
    private = {"owner_token", "worker_id", "lease_expires_at", "request_key", "scope_key", "active_scope", "params", "target"}

    def clean(item, depth=0):
        if depth >= 8:
            return "[truncated]"
        if isinstance(item, dict):
            return {key: clean(value, depth + 1) for key, value in list(item.items())[:100] if key.lower() not in private}
        if isinstance(item, list):
            return [clean(value, depth + 1) for value in item[:100]]
        return item

    return sanitize_log(clean(value))
