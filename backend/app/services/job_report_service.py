"""基于已持久化资源的作业报表，不访问 HCS、不补写状态、不把健康度当作执行结果。

DGC 定义、MRS 原生执行、DataArts 节点与未匹配定义的实例分层查询，禁止父子混算。
Observation.facts 并非历史观测表，未保存的事实不能从告警缺失或作业名称推断。
"""

from collections import Counter
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import String, and_, case, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.deps import Scope
from app.models import AlarmEvent, HcsAccount, HcsProject, InspectionRun, Resource
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow
from app.schemas.job_report import (
    JobCollectionState,
    JobCollectionStatus,
    JobObjectLevel,
    JobPanel,
    JobReportOut,
    JobReportRow,
    JobReportSummary,
    JobResourceType,
    JobSource,
)
from app.services.inspection_health import current_health
from app.services.metric_visibility import current_metrics

JOB_TYPES = ("dgc_realtime_job", "dgc_batch_job", "mrs_job", "dgc_mrs_node")
PANEL_TYPES = {
    "dgc": JOB_TYPES[:2],
    "flink": JOB_TYPES,
    "realtime": ("dgc_realtime_job",),
    "batch": ("dgc_batch_job",),
    "mrs": ("mrs_job", "dgc_mrs_node"),
}
# 仅映射现有源事实中有依据的枚举，不套用其他云服务或其他版本的数字状态码。
STATUS_MAPS = {
    "dgc_realtime_job": {
        "normal": "RUNNING", "exception": "EXCEPTION", "starting": "STARTING",
        "running": "RUNNING", "stopping": "STOPPING", "stopped": "STOPPED", "failed": "FAILED",
        "waiting": "WAITING", "queued": "QUEUED", "pending": "PENDING", "submitted": "SUBMITTED",
        "success": "SUCCEEDED", "succeeded": "SUCCEEDED", "finished": "FINISHED",
        "canceled": "CANCELED", "cancelled": "CANCELED",
    },
    "dgc_batch_job": {
        "success": "SUCCEEDED", "fail": "FAILED", "running": "RUNNING", "running-exception": "EXCEPTION",
        "succeeded": "SUCCEEDED", "failed": "FAILED", "waiting": "WAITING", "queued": "QUEUED",
        "pending": "PENDING", "submitted": "SUBMITTED", "canceled": "CANCELED", "cancelled": "CANCELED",
        "starting": "STARTING", "stopping": "STOPPING", "stopped": "STOPPED", "finished": "FINISHED",
        "pause": "PAUSED", "manual-stop": "CANCELED",
    },
    "dgc_mrs_node": {   # DataArts 表 5-182 Node.status
        "waiting": "WAITING", "running": "RUNNING", "success": "SUCCEEDED", "fail": "FAILED", "skip": "SKIPPED",
        "pause": "PAUSED", "manual-stop": "CANCELED",
    },
}
STATUS_LABELS = {
    "ACCEPTED": "已接受", "NEW_SAVING": "新建保存中", "RUNNING": "运行中",
    "SUCCEEDED": "成功", "FAILED": "失败", "EXCEPTION": "运行异常",
    "STARTING": "启动中", "STOPPING": "停止中", "STOPPED": "已停止", "QUEUED": "排队中",
    "FINISHED": "执行结束（结果未确认）", "UNKNOWN": "结果未定义",
    "WAITING": "等待中", "PENDING": "待处理", "SUBMITTED": "已提交", "NEW": "新建",
    "CANCELED": "已取消", "KILLED": "已终止", "KILLING": "终止中", "NO_DATA": "暂无状态",
    "PAUSED": "暂停", "SKIPPED": "已跳过",
}
PDF_STATUS_MAPS = {
    "dgc_realtime_job": {"NORMAL": "RUNNING", "EXCEPTION": "EXCEPTION", "STARTING": "STARTING",
                         "STOPPING": "STOPPING", "STOPPED": "STOPPED"},
    "dgc_batch_job": {"success": "SUCCEEDED", "fail": "FAILED", "running": "RUNNING",
                      "running-exception": "EXCEPTION", "waiting": "WAITING", "pause": "PAUSED",
                      "manual-stop": "CANCELED"},
    "dgc_mrs_node": {"waiting": "WAITING", "running": "RUNNING", "success": "SUCCEEDED", "fail": "FAILED",
                "skip": "SKIPPED", "pause": "PAUSED", "manual-stop": "CANCELED"},
}
UNRECOGNIZED_PREFIX = "UNRECOGNIZED:"
# 作业 / 节点类型（小写）→ 引擎展示名；MRS 作业的 node_type 来自 DataArts 节点类型（表 5-182）
ENGINE_BY_TYPE = {"flink": "Flink", "sparksubmit": "Spark", "hivesql": "Hive", "mrsflinkjob": "Flink",
                  "mrsspark": "Spark", "mapreduce": "MapReduce", "mrshetuengine": "HetuEngine"}
DETAIL_FIELDS = ("workspace", "nodes", "instance_id", "instance_type", "batch_baseline", "job_name", "node_name",
                 "node_type", "retry_times", "log_path", "job_type", "job_status", "node_status", "instance_status",
                 "plan_time", "submitted_time", "start_time", "end_time", "cluster_id", "cluster", "queue", "queue_name",
                 "job_id", "job_state", "job_result", "elapsed_time", "elapsed_time_unit", "duration_basis",
                 "duration_observed_at", "schedule_status",
                 "definition_present", "data_object", "engine_source", "single_node_type", "node_types", "engines",
                 "instance_query_status", "window_start_ms", "window_end_ms", "listed_last_instance_status",
                 "listed_last_instance_end_time")
DATA_NOTES = [
    "DGC、实时与批作业按父作业定义计数；无近期实例仍保留定义，调度状态不代表执行成功。",
    "MRS 默认按原生执行计数（一条 job_id 一次执行）；DataArts MRS 节点需单独选择，不与父定义混算。",
    "Flink 按实际作业类型、定义或节点引擎识别；默认展示 DGC 定义，可切换原生执行、节点或实例。",
    "展示最新采集快照；最后观测时间为最近采集时间，不等同于执行结束时间。",
    "执行状态与执行结果分别展示；FINISHED 仅表示结束，结果为 SUCCEEDED 才计成功。",
    "未返回的字段显示未采集；原生 MRS、批实例与节点时间按毫秒处理，未知耗时单位不补零。",
    "列表末次实例提示不作为当前执行证据；未匹配定义的 DataArts 实例单独展示。",
    ("批作业 P90 取本轮完整 24h 列表中同工作空间同作业的成功实例起止时长，按 nearest-rank 计算；"
     "默认至少 5 条独立有效实例（本地策略，p90_min_samples 可配），不足时无基线。"),
]



@dataclass(frozen=True)
class JobReportFilters:
    keyword: str | None = None
    status: list[str] | None = None
    status_raw: list[str] | None = None
    resource_type: JobResourceType | None = None
    source: JobSource | None = None
    object_level: JobObjectLevel | None = None
    engine: str | None = None
    job_type: str | None = None
    tenant_id: int | None = None
    project_ref: int | None = None
    lifecycle: Literal["ACTIVE", "DELETED", "ALL"] = "ACTIVE"
    has_alarms: bool | None = None


def _attr_text(key: str):
    return func.nullif(func.trim(cast(Resource.attrs[key].as_string(), String)), "")


def _has_attr(key: str, dialect: str):
    if dialect == "postgresql":
        return cast(Resource.attrs, JSONB).has_key(key)  # noqa: W601  SQLAlchemy JSONB 键存在运算
    return func.json_type(Resource.attrs, f"$.{key}").is_not(None)


def _classification(dialect: str):
    # 迁移前的 DataArts 节点不可冒充 MRS 原生执行；不改写历史资源。
    legacy_node = and_(Resource.resource_type == "mrs_job", or_(
        _attr_text("source") == "dataarts_instance_detail",
        and_(func.coalesce(_attr_text("source"), "") != "mrs_job_execution",
             or_(_has_attr("node_status", dialect), _has_attr("node_type", dialect)))))
    kind = case((legacy_node, "dgc_mrs_node"), else_=Resource.resource_type)
    instance = or_(Resource.attrs["definition_present"].as_boolean().is_(False),
                   _attr_text("data_object") == "INSTANCE", _attr_text("source") == "dataarts_instance")
    level = case((kind == "mrs_job", "EXECUTION"), (kind == "dgc_mrs_node", "NODE"),
                 (instance, "INSTANCE"), else_="DEFINITION")
    source = func.coalesce(_attr_text("source"), case(
        (level == "EXECUTION", "mrs_job_execution"), (level == "NODE", "dataarts_instance_detail"),
        (level == "INSTANCE", "dataarts_instance"), else_="dataarts_definition"))
    return kind, level, source


def _engine_match(value: str, dialect: str):
    value = value.strip().lower()
    tokens = [key for key, name in ENGINE_BY_TYPE.items() if name.lower() == value] + [value]
    scalar_fields = [_attr_text(key) for key in ("engine", "job_type", "type", "node_type", "single_node_type")]
    scalar_fields += [Resource.attrs["definition"][key].as_string() for key in ("engine", "single_node_type")]
    matches: list[ColumnElement] = [func.lower(func.trim(field)).in_(tokens) for field in scalar_fields]
    # JSON 数组逐项精确比较，不能字符串包含匹配，更不能按名称猜测。
    for path in (("engines",), ("node_types",), ("definition", "engines"), ("definition", "node_types")):
        expr = Resource.attrs[path[0]]
        for key in path[1:]:
            expr = expr[key]
        if dialect == "postgresql":
            array = case((func.jsonb_typeof(cast(expr, JSONB)) == "array", cast(expr, JSONB)),
                         else_=cast(literal("[]"), JSONB))
            entries = func.jsonb_array_elements_text(array).table_valued("value").alias()
        else:
            array = case((func.json_type(expr) == "array", expr), else_="[]")
            entries = func.json_each(array).table_valued("value").alias()
        matches.append(select(1).select_from(entries).where(func.lower(func.trim(entries.c.value)).in_(tokens))
                       .correlate(Resource).exists())
    return or_(*matches)


def _array_engine(expr, dialect: str):
    if dialect == "postgresql":
        safe = case((func.jsonb_typeof(cast(expr, JSONB)) == "array", cast(expr, JSONB)),
                    else_=cast(literal("[]"), JSONB))
        size = func.jsonb_array_length(safe)
    else:
        size = func.json_array_length(case((func.json_type(expr) == "array", expr), else_="[]"))
    return case((size > 1, "MULTIPLE"), (size == 1, expr[0].as_string()))


def _selected_level(panel: JobPanel, filters: JobReportFilters) -> JobObjectLevel:
    default_level: JobObjectLevel = ("NODE" if filters.resource_type == "dgc_mrs_node" else
        "EXECUTION" if filters.resource_type == "mrs_job" or panel == "mrs" else "DEFINITION")
    source_levels: dict[str, JobObjectLevel] = {"dataarts_definition": "DEFINITION", "mrs_job_execution": "EXECUTION",
        "dataarts_instance_detail": "NODE", "dataarts_instance": "INSTANCE"}
    return filters.object_level or source_levels.get(filters.source or "") or default_level


def _snapshot_query(scope: Scope, panel: JobPanel, dialect: str, filters: JobReportFilters | None = None):
    filters = filters or JobReportFilters()
    kind, level, source_kind = _classification(dialect)
    state_keys = {"mrs_job": "job_state", "dgc_mrs_node": "node_status",
                  "dgc_realtime_job": "job_status", "dgc_batch_job": "instance_status"}
    state = case(*[(kind == name, cast(Resource.attrs[key].as_string(), String)) for name, key in state_keys.items()])
    has_state = case(*[(kind == name, _has_attr(key, dialect)) for name, key in state_keys.items()], else_=False)
    current_contract = or_(Resource.attrs["definition_present"].as_boolean().is_(True),
                           _attr_text("data_object") == "DEFINITION",
                           _attr_text("source").in_(("dataarts_definition", "mrs_job_execution")))
    source = case((or_(has_state, current_contract), state), else_=Resource.status_raw)
    raw = func.lower(func.trim(source))
    fallback = case((or_(raw.is_(None), raw == ""), "NO_DATA"), else_=source)
    mapped = case(*[(kind == name, case(mapping, value=raw, else_=fallback))
                    for name, mapping in STATUS_MAPS.items()], else_=fallback)
    known = case(*[(kind == name, raw.in_(tuple(mapping))) for name, mapping in STATUS_MAPS.items()], else_=False)
    unrecognized = case((source.in_(tuple(STATUS_LABELS)), UNRECOGNIZED_PREFIX + source), else_=fallback)
    mapped = case(*[(and_(kind == name, has_state), case(mapping, value=source, else_=unrecognized))
                    for name, mapping in PDF_STATUS_MAPS.items()], else_=mapped)
    known = case(*[(and_(kind == name, has_state), source.in_(tuple(mapping)))
                   for name, mapping in PDF_STATUS_MAPS.items()], else_=known)
    result = cast(Resource.attrs["job_result"].as_string(), String)
    native_states = ("NEW", "NEW_SAVING", "SUBMITTED", "ACCEPTED", "RUNNING", "FINISHED", "FAILED", "KILLED")
    native = case((and_(source == "FINISHED", result == "SUCCEEDED"), "SUCCEEDED"),
                  (and_(source == "FINISHED", result == "FAILED"), "FAILED"),
                  (and_(source == "FINISHED", result == "KILLED"), "KILLED"),
                  (source.in_(native_states), source), else_=unrecognized)
    display = case((kind == "mrs_job", native), else_=mapped)
    known = case((kind == "mrs_job", source.in_(native_states)), else_=known)
    job_type = func.coalesce(_attr_text("job_type"), _attr_text("type"), _attr_text("node_type"),
                             _attr_text("single_node_type"), Resource.attrs["definition"]["single_node_type"].as_string())
    engine: ColumnElement = func.coalesce(_attr_text("engine"), case(ENGINE_BY_TYPE, value=func.lower(job_type)),
                           *[case(ENGINE_BY_TYPE, value=func.lower(_attr_text(key)))
                             for key in ("node_type", "single_node_type")])
    engine = func.coalesce(engine, Resource.attrs["definition"]["engine"].as_string(),
        case(ENGINE_BY_TYPE, value=func.lower(Resource.attrs["definition"]["single_node_type"].as_string())),
        *[_array_engine(expr, dialect) for expr in
          (Resource.attrs["engines"], Resource.attrs["definition"]["engines"])])
    engine = case(ENGINE_BY_TYPE, value=func.lower(func.trim(engine)), else_=engine)
    alarms = scope.apply(select(AlarmEvent.resource_ref, AlarmEvent.tenant_id,
                                func.count().label("active_alarms")), AlarmEvent.tenant_id).where(
        AlarmEvent.status.in_(ACTIVE_STATUSES)).group_by(AlarmEvent.resource_ref, AlarmEvent.tenant_id).subquery()
    q = scope.apply(select(
        Resource.id, Resource.service, kind.label("resource_type"), Resource.status_raw,
        Resource.last_seen.label("updated_at"), level.label("object_level"), source_kind.label("source"),
        display.label("display_status"), func.coalesce(known, False).label("status_known"),
        state.label("execution_state"), case((kind == "mrs_job", result)).label("execution_result"),
        job_type.label("job_type"), engine.label("engine"),
        func.coalesce(alarms.c.active_alarms, 0).label("active_alarms"),
    ).outerjoin(alarms, and_(alarms.c.resource_ref == Resource.id,
                            alarms.c.tenant_id.is_not_distinct_from(Resource.tenant_id))), Resource.tenant_id)
    selected_level = _selected_level(panel, filters)
    q = q.where(Resource.resource_type.in_(JOB_TYPES), kind.in_(PANEL_TYPES[panel]),
                level == selected_level, Resource.scope_excluded.is_(False))
    if filters.resource_type:
        q = q.where(kind == filters.resource_type)
    if filters.source:
        q = q.where(source_kind == filters.source)
    if panel == "flink":
        q = q.where(_engine_match("flink", dialect))
    return q, display, engine, job_type, alarms


def _filtered_snapshot(scope: Scope, panel: JobPanel, filters: JobReportFilters, dialect: str):
    q, display, engine, job_type, alarms = _snapshot_query(scope, panel, dialect, filters)
    if filters.lifecycle != "ALL":
        q = q.where(Resource.lifecycle == filters.lifecycle)
    for value, column in ((filters.tenant_id, Resource.tenant_id),
                          (filters.project_ref, Resource.project_ref)):
        if value is not None:
            q = q.where(column == value)
    if filters.keyword and filters.keyword.strip():
        keyword = filters.keyword.strip()
        q = q.where(or_(Resource.name.icontains(keyword, autoescape=True),
                        Resource.resource_id.icontains(keyword, autoescape=True)))
    if filters.status:
        q = q.where(display.in_(filters.status))
    if filters.status_raw:
        q = q.where(Resource.status_raw.in_(filters.status_raw))
    if filters.engine is not None:
        q = q.where(_engine_match(filters.engine, dialect))
    for text_value, text_column in ((filters.job_type, job_type),):
        if text_value is not None:
            q = q.where(func.lower(text_column) == text_value.strip().lower())
    if filters.has_alarms is not None:
        count = func.coalesce(alarms.c.active_alarms, 0)
        q = q.where(count > 0 if filters.has_alarms else count == 0)
    return q.subquery()


def _status_details(attrs: dict) -> dict:
    details = {key: attrs[key] for key in DETAIL_FIELDS if key in attrs}
    if "submitted_time" not in details and "submit_time" in attrs:
        details["submitted_time"] = attrs["submit_time"]  # 兼容既有 DataArts 快照字段名，不覆写显式空值。
    definition = attrs.get("definition")
    if isinstance(definition, dict):
        details["definition"] = {key: definition[key] for key in
                                 ("single_node_type", "node_types", "engines", "engine") if key in definition}
    if isinstance(details.get("cluster"), dict):
        details["cluster"] = {key: details["cluster"][key] for key in ("id", "name") if key in details["cluster"]}
    # 节点仅展示当前采集器已保存的名称和状态，不透传将来可能携带的连接信息。
    if "nodes" in details:
        details["nodes"] = [{key: node[key] for key in ("name", "status") if key in node}
                            for node in details["nodes"] if isinstance(node, dict)] if isinstance(
                                details["nodes"], list) else None
    return details


def get_job_report(db: Session, scope: Scope, panel: JobPanel, filters: JobReportFilters,
                   *, page: int = 1, page_size: int = 20) -> JobReportOut:
    snapshot = _filtered_snapshot(scope, panel, filters, db.get_bind().dialect.name)
    unknown = and_(snapshot.c.display_status != "NO_DATA",
                   or_(snapshot.c.status_known.is_(False), snapshot.c.display_status.in_(("UNKNOWN", "FINISHED"))))
    groups = db.execute(select(
        snapshot.c.display_status, snapshot.c.service, snapshot.c.resource_type, snapshot.c.engine,
        snapshot.c.source, snapshot.c.object_level, func.count().label("total"),
        func.sum(case((unknown, 1), else_=0)).label("unknown_total"),
        func.sum(case((snapshot.c.display_status == "NO_DATA", 1), else_=0)).label("no_data_total"),
        func.sum(snapshot.c.active_alarms).label("active_alarms"),
        func.max(snapshot.c.updated_at).label("latest_update_at"),
    ).group_by(snapshot.c.display_status, snapshot.c.service, snapshot.c.resource_type, snapshot.c.engine,
               snapshot.c.source, snapshot.c.object_level)).all()
    total = sum(row.total for row in groups)
    statuses: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    engines: Counter[str] = Counter()
    types: Counter[str] = Counter()
    collection_sources: Counter[str] = Counter()
    levels: Counter[str] = Counter()
    for row in groups:
        statuses[row.display_status] += row.total
        sources[row.service] += row.total
        engines[row.engine or "UNKNOWN"] += row.total
        types[row.resource_type] += row.total
        collection_sources[row.source] += row.total
        levels[row.object_level] += row.total
    summary = JobReportSummary(
        total=total, status_counts=dict(statuses), by_source=dict(sources), by_engine=dict(engines), by_type=dict(types),
        by_collection_source=dict(collection_sources), by_object_level=dict(levels),
        unknown_total=sum(row.unknown_total for row in groups),
        no_data_total=sum(row.no_data_total for row in groups),
        active_alarms=sum(row.active_alarms for row in groups),
        latest_update_at=max((row.latest_update_at for row in groups), default=None),
        data_status="AVAILABLE" if total else "NO_DATA",
    )
    # 先在 SQL 中分页，再加载详情和显示名；不查询租户的凭据列。
    paged = select(snapshot).order_by(snapshot.c.updated_at.desc(), snapshot.c.id.desc()).offset(
        (page - 1) * page_size).limit(page_size).subquery()
    items = _read_rows(db, paged)
    return JobReportOut(panel=panel, summary=summary, items=items, total=total, page=page, page_size=page_size,
                        data_notes=DATA_NOTES, collection_status=job_collection_status(db, scope, panel, filters, total))


def list_job_report_rows(db: Session, scope: Scope, panel: JobPanel,
                         filters: JobReportFilters) -> list[JobReportRow]:
    """与列表使用相同筛选，一次查询取得导出全部行。"""
    snapshot = _filtered_snapshot(scope, panel, filters, db.get_bind().dialect.name)
    return _read_rows(db, select(snapshot).subquery())


def _read_rows(db: Session, selected) -> list[JobReportRow]:
    rows = db.execute(select(
        Resource, selected.c.display_status, selected.c.status_known, selected.c.job_type, selected.c.engine,
        selected.c.execution_state, selected.c.execution_result, selected.c.source, selected.c.object_level,
        selected.c.resource_type.label("report_resource_type"),
        selected.c.active_alarms, HcsAccount.name.label("tenant_name"),
        HcsProject.project_id, HcsProject.project_name,
    ).join(selected, Resource.id == selected.c.id).outerjoin(HcsAccount, HcsAccount.id == Resource.tenant_id).outerjoin(
        HcsProject, and_(HcsProject.id == Resource.project_ref, HcsProject.account_id == Resource.tenant_id),
    ).order_by(selected.c.updated_at.desc(), selected.c.id.desc())).all()
    items = []
    now = utcnow()
    for row in rows:
        res = row.Resource
        label = (STATUS_LABELS.get(row.display_status, row.display_status) if row.status_known
                 or row.display_status == "NO_DATA" else row.display_status.removeprefix(UNRECOGNIZED_PREFIX))
        items.append(JobReportRow(
            id=res.id, resource_id=res.resource_id, name=res.name, service=res.service, resource_type=row.report_resource_type,
            job_type=row.job_type, engine=None if row.engine == "MULTIPLE" else row.engine,
            status_raw=res.status_raw, display_status=row.display_status,
            execution_state=row.execution_state, execution_result=row.execution_result,
            source=row.source, object_level=row.object_level, engines=_engines(res.attrs or {}, row.engine),
            status_label=label, status_known=row.status_known, health=current_health(res, now), lifecycle=res.lifecycle,
            tenant_id=res.tenant_id, tenant_name=row.tenant_name, project_ref=res.project_ref,
            project_id=row.project_id, project_name=row.project_name, region=res.region,
            updated_at=res.last_seen, status_since=res.status_since, status_details=_status_details(res.attrs or {}),
            metrics=_row_metrics(res, now), active_alarms=row.active_alarms,
        ))
    return items


def _row_metrics(resource: Resource, now) -> dict:
    metrics = current_metrics(resource, now=now)
    attrs = resource.attrs or {}
    unit = attrs.get("elapsed_time_unit")
    known_unit = isinstance(unit, str) and unit.lower() in ("milliseconds", "seconds")
    if (resource.resource_type == "mrs_job" and attrs.get("elapsed_time") is not None and not known_unit
            and attrs.get("duration_basis") not in ("start_end_milliseconds", "start_now_milliseconds")):
        # 旧快照或未知单位不能借用默认零冒充已确认时长；有明确起止依据的时长仍可展示。
        metrics["run_minutes"] = None
        if "elapsed_min" in metrics:
            metrics["elapsed_min"] = None
    return metrics


def _engines(attrs: dict, engine: str | None) -> list[str]:
    definition = attrs.get("definition")
    definition = definition if isinstance(definition, dict) else {}
    values = [engine if engine != "MULTIPLE" else None, definition.get("engine")]
    for data in (attrs, definition):
        for key in ("node_type", "single_node_type"):
            value = data.get(key)
            if isinstance(value, str) and value.lower() in ENGINE_BY_TYPE:
                values.append(value)
    if isinstance(attrs.get("job_type"), str) and attrs["job_type"].lower() in ENGINE_BY_TYPE:
        values.append(attrs["job_type"])
    for data in (attrs, definition):
        for key in ("engines", "node_types"):
            if isinstance(data.get(key), list):
                values.extend(value for value in data[key] if key == "engines" or
                              isinstance(value, str) and value.lower() in ENGINE_BY_TYPE)
    return sorted({ENGINE_BY_TYPE.get(value.strip().lower(), value.strip()) for value in values
                   if isinstance(value, str) and value.strip() and value.strip().upper() not in ("BATCH", "REAL_TIME")})


def job_collection_status(db: Session, scope: Scope, panel: JobPanel, filters: JobReportFilters,
                          total: int) -> JobCollectionStatus:
    if total:
        return JobCollectionStatus(state="AVAILABLE", message="当前筛选的最新采集快照")
    # 空数据诊断只查授权范围的本地记录，不读取任务参数、凭据或原始错误，不触发采集。
    dialect = db.get_bind().dialect.name
    kind, _, _ = _classification(dialect)
    q = scope.apply(select(func.count()).select_from(Resource), Resource.tenant_id).where(
        Resource.resource_type.in_(JOB_TYPES), kind.in_(PANEL_TYPES[panel]))
    if filters.tenant_id is not None:
        q = q.where(Resource.tenant_id == filters.tenant_id)
    if filters.project_ref is not None:
        q = q.where(Resource.project_ref == filters.project_ref)
    has_local = bool(db.scalar(q))
    from app.services.catalog import TASK_TYPES

    selected_level = _selected_level(panel, filters)
    level_types = {"DEFINITION": JOB_TYPES[:2], "INSTANCE": JOB_TYPES[:2],
                   "EXECUTION": ("mrs_job",), "NODE": ("dgc_mrs_node",)}
    types = (filters.resource_type,) if filters.resource_type else tuple(
        kind for kind in PANEL_TYPES[panel] if kind in level_types[selected_level])
    codes = [code for code, resource_types in TASK_TYPES.items() if set(types).intersection(resource_types)]
    runs = scope.apply(select(InspectionRun.id, InspectionRun.task_code, InspectionRun.tenant_id,
        InspectionRun.project_ref, InspectionRun.status, InspectionRun.started_at, InspectionRun.stats),
        InspectionRun.tenant_id).where(
        InspectionRun.task_code.in_(codes))
    if filters.tenant_id is not None:
        runs = runs.where(InspectionRun.tenant_id == filters.tenant_id)
    if filters.project_ref is not None:
        runs = runs.where(InspectionRun.project_ref == filters.project_ref)
    latest = db.execute(runs.order_by(InspectionRun.started_at.desc(), InspectionRun.id.desc()).limit(1)).first()
    if latest is None and has_local:
        return JobCollectionStatus(state="NO_MATCH", message="当前查询范围无匹配对象，可调整来源、层级或筛选条件")
    if latest is None:
        return JobCollectionStatus(state="NOT_COLLECTED", message="当前范围尚无作业采集记录，不能据此判断云端无任务")
    reasons = _dataarts_collection_reason(latest.stats, types, selected_level,
        precise=bool(filters.project_ref is not None and (filters.tenant_id or scope.single) and len(codes) == 1))
    if reasons is not None:
        if latest.status != "SUCCESS" and reasons.state == "CLOUD_EMPTY":
            reasons.state = "FAILED" if latest.status in ("FAILED", "PARTIAL", "TIMEOUT") else "UNKNOWN"
            reasons.message = "最近一次采集未完整成功，不能将局部空结果当作云端无任务"
        reasons.last_attempt_at, reasons.last_run_status = latest.started_at, latest.status
        return reasons
    if has_local:
        return JobCollectionStatus(state="NO_MATCH", message="当前查询范围无匹配对象，可调整来源、层级或筛选条件")
    states: dict[str, tuple[JobCollectionState, str]] = {"RUNNING": ("COLLECTING", "当前范围采集中，尚无作业快照"),
              "FAILED": ("FAILED", "当前范围最近采集失败，暂无作业快照"),
              "TIMEOUT": ("FAILED", "当前范围最近采集超时，暂无作业快照"),
              "PARTIAL": ("FAILED", "当前范围最近采集不完整，暂无作业快照")}
    state, message = states.get(latest.status, ("UNKNOWN", "当前范围无作业快照；尚无可确认的云端空结果"))
    # resource_total=0 不能证明完整的云端空结果，须有显式层级证据；历史记录不作推断。
    if latest.status == "SUCCESS" and filters.project_ref is not None and (filters.tenant_id or scope.single):
        marker = latest.stats.get("job_report_collection") if isinstance(latest.stats, dict) else None
        wanted = selected_level
        if (isinstance(marker, dict) and marker.get("state") == "EMPTY" and marker.get("complete") is True
                and marker.get("object_level") == wanted and len(codes) == 1):
            state, message = "CLOUD_EMPTY", "本次云端完整查询返回空结果（仅当前项目与对象层级）"
    return JobCollectionStatus(state=state, message=message, last_attempt_at=latest.started_at,
                               last_run_status=latest.status)


DATAARTS_REASONS = frozenset(("DEFAULT_WORKSPACE_EMPTY", "EMPTY_DEFINITIONS", "SCOPE_FILTERED",
    "WORKSPACE_EXCLUDED", "NO_RECENT_INSTANCES", "LIST_FAILED", "INSTANCE_QUERY_FAILED", "AVAILABLE"))


def _dataarts_collection_reason(stats: dict, types, level: JobObjectLevel,
                               *, precise: bool) -> JobCollectionStatus | None:
    data = stats.get("dataarts") if isinstance(stats, dict) else None
    entries = data.get("workspaces") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None
    job_types = {"REAL_TIME" for kind in types if kind == "dgc_realtime_job"}
    job_types.update("BATCH" for kind in types if kind == "dgc_batch_job")
    reasons = Counter(entry["reason"] for entry in entries if isinstance(entry, dict)
        and isinstance(entry.get("job_type"), str) and entry["job_type"] in job_types
        and isinstance(entry.get("reason"), str) and entry["reason"] in DATAARTS_REASONS)
    if not reasons:
        return None
    state: JobCollectionState = "UNKNOWN"
    if reasons.keys() & {"LIST_FAILED", "INSTANCE_QUERY_FAILED"}:
        state, message = "FAILED", "最近一次 DataArts 定义或实例查询失败，不能据此判断无任务"
    elif reasons.keys() & {"SCOPE_FILTERED", "WORKSPACE_EXCLUDED"}:
        state, message = "NO_MATCH", "最近一次 DataArts 采集被任务或工作空间范围过滤，当前范围无匹配对象"
    elif reasons.keys() <= {"DEFAULT_WORKSPACE_EMPTY", "EMPTY_DEFINITIONS"} and level == "DEFINITION":
        state = "CLOUD_EMPTY" if precise else "UNKNOWN"
        message = "最近一次 DataArts 云端查询返回空定义；默认工作空间为空不代表其他工作空间无任务"
    elif "NO_RECENT_INSTANCES" in reasons:
        state = "CLOUD_EMPTY" if precise and level == "INSTANCE" else "UNKNOWN"
        message = "最近一次 DataArts 查询近24h无实例；父作业定义仍独立计数"
    else:
        state, message = "NO_MATCH", "最近一次 DataArts 采集有返回，当前来源、层级或引擎筛选无匹配对象"
    return JobCollectionStatus(state=state, message=message, reasons=dict(reasons))
