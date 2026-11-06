"""基于已持久化资源的作业报表，不访问 HCS、不补写状态、不把健康度当作执行结果。

状态口径来自 collectors/dataarts.py、collectors/mrs.py 和对应规则；MRS 作业即 DataArts 作业实例中的 MRS 类节点。
Observation.facts 并非历史观测表，未保存的事实不能从告警缺失或作业名称推断。
"""

from collections import Counter
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import String, and_, case, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.core.deps import Scope
from app.models import AlarmEvent, HcsAccount, HcsProject, Resource
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow
from app.schemas.job_report import JobPanel, JobReportOut, JobReportRow, JobReportSummary, JobResourceType
from app.services.inspection_health import current_health
from app.services.metric_visibility import current_metrics

JOB_TYPES = ("dgc_realtime_job", "dgc_batch_job", "mrs_job")
PANEL_TYPES = {
    "dgc": JOB_TYPES[:2],
    "flink": JOB_TYPES,
    "realtime": ("dgc_realtime_job",),
    "batch": ("dgc_batch_job",),
    "mrs": ("mrs_job",),
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
    "mrs_job": {   # DataArts 表 5-182 Node.status
        "waiting": "WAITING", "running": "RUNNING", "success": "SUCCEEDED", "fail": "FAILED", "skip": "SKIPPED",
        "pause": "PAUSED", "manual-stop": "CANCELED",
    },
}
STATUS_LABELS = {
    "RUNNING": "运行中", "SUCCEEDED": "成功", "FAILED": "失败", "EXCEPTION": "运行异常",
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
    "mrs_job": {"waiting": "WAITING", "running": "RUNNING", "success": "SUCCEEDED", "fail": "FAILED",
                "skip": "SKIPPED", "pause": "PAUSED", "manual-stop": "CANCELED"},
}
UNRECOGNIZED_PREFIX = "UNRECOGNIZED:"
# 作业 / 节点类型（小写）→ 引擎展示名；MRS 作业的 node_type 来自 DataArts 节点类型（表 5-182）
ENGINE_BY_TYPE = {"flink": "Flink", "sparksubmit": "Spark", "hivesql": "Hive", "mrsflinkjob": "Flink",
                  "mrsspark": "Spark", "mapreduce": "MapReduce", "mrshetuengine": "HetuEngine"}
DETAIL_FIELDS = ("workspace", "nodes", "instance_id", "instance_type", "batch_baseline", "job_name", "node_name",
                 "node_type", "retry_times", "log_path", "job_type", "job_status", "node_status", "instance_status",
                 "plan_time", "submitted_time", "start_time", "end_time")
DATA_NOTES = [
    "展示各作业最近一次采集数据，统计按当前筛选条件汇总。",
    "最后观测时间为最近采集时间。",
    "执行状态与执行结果分别展示；未返回的字段显示为未采集。",
    "MRS 作业取自 DataArts 作业实例详情中类型为 MRSSpark、MapReduce、MRSFlinkJob、MRSHetuEngine 的节点；"
    "只覆盖 DataArts 编排的作业，不包含直接在 MRS 控制台提交的作业。",
    "DataArts 实时 Date 字段未确认数值单位时保留原文；批实例与 MRS 节点的毫秒时间戳转为日期。",
    ("批作业 P90 取本轮完整 24h 列表中同工作空间同作业的成功实例起止时长，按 nearest-rank 计算；"
     "默认至少 5 条独立有效实例（本地策略，p90_min_samples 可配），不足时无基线；"
     "不使用 executeTime 或上游未定义的 p90_minutes。"),
]


@dataclass(frozen=True)
class JobReportFilters:
    keyword: str | None = None
    status: list[str] | None = None
    status_raw: list[str] | None = None
    resource_type: JobResourceType | None = None
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


def _snapshot_query(scope: Scope, panel: JobPanel, dialect: str):
    state_keys = {"mrs_job": "node_status", "dgc_realtime_job": "job_status", "dgc_batch_job": "instance_status"}
    state = case(
        *[(Resource.resource_type == kind, cast(Resource.attrs[key].as_string(), String))
          for kind, key in state_keys.items()],
    )
    has_state = case(*[(Resource.resource_type == kind, _has_attr(key, dialect))
                       for kind, key in state_keys.items()], else_=False)
    # 新快照显式保存空状态时，不能复用流水线未覆盖的旧 status_raw 并显示历史成功。
    source = case((has_state, state), else_=Resource.status_raw)
    raw = func.lower(func.trim(source))
    fallback = case((or_(raw.is_(None), raw == ""), "NO_DATA"), else_=source)
    mapped = case(*[(Resource.resource_type == kind, case(mapping, value=raw, else_=fallback))
                    for kind, mapping in STATUS_MAPS.items()], else_=fallback)
    mapped_known = case(*[(Resource.resource_type == kind, raw.in_(tuple(mapping)))
                          for kind, mapping in STATUS_MAPS.items()], else_=False)
    # 当前快照按上传 PDF 的大小写与枚举识别；旧 status_raw 保留既有兼容展示。
    # 未识别原文若恰与标准展示码重名，隔离聚合键，避免把未知 SUCCEEDED 计入成功卡片。
    unrecognized = case((source.in_(tuple(STATUS_LABELS)), UNRECOGNIZED_PREFIX + source), else_=fallback)
    mapped = case(*[(and_(Resource.resource_type == kind, has_state),
                     case(mapping, value=source, else_=unrecognized)) for kind, mapping in PDF_STATUS_MAPS.items()],
                  else_=mapped)
    mapped_known = case(*[(and_(Resource.resource_type == kind, has_state), source.in_(tuple(mapping)))
                          for kind, mapping in PDF_STATUS_MAPS.items()], else_=mapped_known)
    display, known = mapped, mapped_known
    job_type = func.coalesce(_attr_text("job_type"), _attr_text("type"), _attr_text("node_type"))
    engine = func.coalesce(_attr_text("engine"), case(ENGINE_BY_TYPE, value=func.lower(job_type)))
    # 预聚合避免节点级多告警将资源重复成多行；同时按租户关联，防御历史错误关联。
    alarms = scope.apply(select(AlarmEvent.resource_ref, AlarmEvent.tenant_id,
                                func.count().label("active_alarms")), AlarmEvent.tenant_id).where(
        AlarmEvent.status.in_(ACTIVE_STATUSES)).group_by(AlarmEvent.resource_ref, AlarmEvent.tenant_id).subquery()
    q = scope.apply(select(
        Resource.id, Resource.service, Resource.resource_type, Resource.status_raw, Resource.last_seen.label("updated_at"),
        display.label("display_status"), func.coalesce(known, False).label("status_known"),
        state.label("execution_state"),
        job_type.label("job_type"), engine.label("engine"),
        func.coalesce(alarms.c.active_alarms, 0).label("active_alarms"),
    ).outerjoin(alarms, and_(alarms.c.resource_ref == Resource.id,
                            alarms.c.tenant_id.is_not_distinct_from(Resource.tenant_id))), Resource.tenant_id)
    q = q.where(Resource.resource_type.in_(PANEL_TYPES[panel]))
    if panel == "flink":
        q = q.where(or_(*(func.lower(_attr_text(key)) == "flink" for key in ("engine", "job_type", "type")),
                        func.lower(_attr_text("node_type")) == "mrsflinkjob"))
    return q, display, engine, job_type, alarms


def _filtered_snapshot(scope: Scope, panel: JobPanel, filters: JobReportFilters, dialect: str):
    q, display, engine, job_type, alarms = _snapshot_query(scope, panel, dialect)
    if filters.lifecycle != "ALL":
        q = q.where(Resource.lifecycle == filters.lifecycle)
    for value, column in ((filters.resource_type, Resource.resource_type), (filters.tenant_id, Resource.tenant_id),
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
    for text_value, text_column in ((filters.engine, engine), (filters.job_type, job_type)):
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
                   or_(snapshot.c.status_known.is_(False), snapshot.c.display_status == "UNKNOWN"))
    groups = db.execute(select(
        snapshot.c.display_status, snapshot.c.service, snapshot.c.resource_type, snapshot.c.engine, func.count().label("total"),
        func.sum(case((unknown, 1), else_=0)).label("unknown_total"),
        func.sum(case((snapshot.c.display_status == "NO_DATA", 1), else_=0)).label("no_data_total"),
        func.sum(snapshot.c.active_alarms).label("active_alarms"),
        func.max(snapshot.c.updated_at).label("latest_update_at"),
    ).group_by(snapshot.c.display_status, snapshot.c.service, snapshot.c.resource_type, snapshot.c.engine)).all()
    total = sum(row.total for row in groups)
    statuses: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    engines: Counter[str] = Counter()
    types: Counter[str] = Counter()
    for row in groups:
        statuses[row.display_status] += row.total
        sources[row.service] += row.total
        engines[row.engine or "UNKNOWN"] += row.total
        types[row.resource_type] += row.total
    summary = JobReportSummary(
        total=total, status_counts=dict(statuses), by_source=dict(sources), by_engine=dict(engines), by_type=dict(types),
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
                        data_notes=DATA_NOTES)


def list_job_report_rows(db: Session, scope: Scope, panel: JobPanel,
                         filters: JobReportFilters) -> list[JobReportRow]:
    """与列表使用相同筛选，一次查询取得导出全部行。"""
    snapshot = _filtered_snapshot(scope, panel, filters, db.get_bind().dialect.name)
    return _read_rows(db, select(snapshot).subquery())


def _read_rows(db: Session, selected) -> list[JobReportRow]:
    rows = db.execute(select(
        Resource, selected.c.display_status, selected.c.status_known, selected.c.job_type, selected.c.engine,
        selected.c.execution_state,
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
            id=res.id, resource_id=res.resource_id, name=res.name, service=res.service, resource_type=res.resource_type,
            job_type=row.job_type, engine=row.engine, status_raw=res.status_raw, display_status=row.display_status,
            execution_state=row.execution_state,
            status_label=label, status_known=row.status_known, health=current_health(res, now), lifecycle=res.lifecycle,
            tenant_id=res.tenant_id, tenant_name=row.tenant_name, project_ref=res.project_ref,
            project_id=row.project_id, project_name=row.project_name, region=res.region,
            updated_at=res.last_seen, status_since=res.status_since, status_details=_status_details(res.attrs or {}),
            metrics=current_metrics(res, now=now), active_alarms=row.active_alarms,
        ))
    return items
