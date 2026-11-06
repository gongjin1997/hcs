"""ECS 真实采样入库、快照和趋势；读取页面不隐式调用云端。"""
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from math import ceil, isfinite
from typing import cast

from sqlalchemy import Integer, case, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.deps import Scope
from app.core.response import BizError, ErrorCode, not_found
from app.metrics.ecs_diagnostics import INCOMPLETE, REASONS, diagnostic_message, unavailable_reason
from app.metrics.ecs_readings import EcsReading as Reading
from app.metrics.ecs_readings import ecs_source
from app.models import EcsMetricSample, HcsAccount, HcsProject, InspectionTask, Resource
from app.models.base import utcnow
from app.schemas.ecs import (
    EcsDiagnostic,
    EcsHistoryOut,
    EcsHistoryPoint,
    EcsHistorySeries,
    EcsInstanceOut,
    EcsInstancesOut,
    EcsLatest,
    MetricName,
    TimestampSemantics,
)
from app.services import collect_switch
from app.services.inspection_health import current_health

METRICS = {"cpu_util": "CPU 使用率", "mem_util": "内存使用率", "disk_util": "最满分区使用率"}
RETENTION_DAYS = 90
STATES = {"FRESH", "STALE", "NO_DATA", "DISABLED", "NOT_SELECTED", "SOURCE_UNCONFIGURED", "MAPPING_MISSING", "ERROR"}
DIAGNOSTICS = {
    "NO_SOURCE": "未配置可用指标源，使用率暂无数据",
    "SOURCE_UNCONFIGURED": "未配置可用指标源，使用率暂无数据",
    "MAPPING_MISSING": "尚未配置此租户的 ECS 指标映射",
    "NO_SERIES": "未找到匹配的指标序列，请核对实例维度和映射",
    "NO_DATA": "本次查询没有实际采样，请核对采样窗口和监控端状态",
    "PARTIAL": "部分指标缺失，已保留实际返回的采样",
    "ERROR": "指标查询失败，请查看对应的任务运行记录",
    "STALE": "最近采样已过期，请检查指标源和采样任务",
    "DISABLED": "指标采样已关闭，历史数据保留",
    "NOT_SELECTED": "此实例未在采样范围内，历史数据保留",
    "FRESH": "查询结果可用",
    "AVAILABLE": "查询结果可用",
    "OK": "查询结果可用",
}


def parse_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return at.astimezone(UTC) if at.tzinfo is not None else None


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        val = float(value)
    except (ValueError, OverflowError):
        return None
    return val if isfinite(val) and 0 <= val <= 100 else None


def policy_dict(db: Session, tenant_id: int | None) -> dict:
    from app.services import collection_policy

    policy = collection_policy.effective(db, tenant_id, "ecs_server")
    return policy.model_dump(mode="json") if hasattr(policy, "model_dump") else dict(policy)


def included(resource: Resource, policy: dict) -> bool:
    if resource.lifecycle != "ACTIVE" or not resource.monitored or resource.scope_excluded:
        return False
    selection = policy.get("selection") or {}
    mode = selection.get("mode", "all")
    if mode == "critical":
        return resource.critical
    if mode == "selected":
        return resource.id in selection.get("resource_ids", [])
    return mode == "all"


def config_signature(tenant: object, policy: dict, source: str) -> str:
    # 只存摘要；查询、日志及导出不含端点、查询表达式或凭据。
    payload = [source, getattr(tenant, "metrics_source", None), getattr(tenant, "endpoints", None),
               getattr(tenant, "prometheus_url", None), policy.get("prometheus"),
               policy.get("metric_keys"), getattr(tenant, "id", None), getattr(tenant, "secret_cipher", None),
               getattr(tenant, "access_key", None), getattr(tenant, "domain_name", None)]
    return sha256(dumps(payload, ensure_ascii=True, sort_keys=True, default=str).encode()).hexdigest()


def timestamp_semantics(source: str, metric: str, diagnostic: dict | None) -> TimestampSemantics:
    """只传播 provider 证据；不根据表达式、标签或本地采集时间推断物理采样。"""
    metrics = diagnostic.get("metrics") if isinstance(diagnostic, dict) else None
    entry = metrics.get(metric) if isinstance(metrics, dict) else None
    value = entry.get("timestamp_semantics") if isinstance(entry, dict) else None
    if value in ("evaluation", "source_sample"):
        return cast(TimestampSemantics, value)
    # CES 数据点的 timestamp 字段来自显式选择的旧兼容协议；不代表已认证 HCS 版次。
    return "source_sample" if source == "CES" else "unknown"


def save_readings(db: Session, resources: Sequence[Resource], readings: Mapping[str, Iterable[Reading]],
                  collected_at: datetime, *, diagnostic: dict | None = None,
                  signature: str | None = None) -> tuple[dict[int, list[Reading]], int]:
    """批量幂等写入；同源同一采样点不重复累计，也不以新查询时间伪造新点。"""
    accepted: dict[int, list[Reading]] = defaultdict(list)
    proposed: dict[tuple, tuple[dict, Reading]] = {}
    rejected = 0
    conflicts = set()
    cutoff = collected_at - timedelta(days=RETENTION_DAYS)
    for resource in resources:
        if (resource.resource_type != "ecs_server" or resource.lifecycle != "ACTIVE" or not resource.monitored
                or resource.scope_excluded):
            continue
        for point in readings.get(resource.resource_id, []):
            if point.metric == "boot_time":
                continue
            value = number(point.value)
            at = point.observed_at
            if (point.metric not in METRICS or value is None or point.unit != "%"
                    or point.source not in ("PROMETHEUS", "CES") or at.tzinfo is None
                    or not cutoff <= at <= collected_at + timedelta(seconds=30)
                    or isinstance(point.period_sec, bool) or not isinstance(point.period_sec, int) or point.period_sec < 0):
                rejected += 1
                continue
            key = (resource.id, point.metric, point.source, at.astimezone(UTC))
            if key in conflicts:
                rejected += 1
                continue
            row: dict = {"resource_ref": resource.id, "tenant_id": resource.tenant_id, "project_ref": resource.project_ref,
                   "metric": point.metric, "value": value, "source": point.source, "unit": "%", "observed_at": key[3],
                   "collected_at": collected_at, "period_sec": point.period_sec,
                   "dimensions": {k: v for k, v in (point.dimensions or {}).items()
                                  if k in ("mount_point", "mountpoint") and isinstance(v, str) and len(v) <= 255}}
            if diagnostic is not None or signature is not None:
                # 这是本地元数据命名空间，绝不写回 point.dimensions 原始标签。
                row["dimensions"]["__ecs_monitoring"] = {"version": 1,
                    "timestamp_semantics": timestamp_semantics(point.source, point.metric, diagnostic),
                    "config_signature": signature}
            if key in proposed and (proposed[key][0]["value"] != value
                                    or proposed[key][0]["dimensions"] != row["dimensions"]):
                rejected += 1
                proposed.pop(key)
                conflicts.add(key)
            else:
                proposed[key] = (row, point)
    if not proposed:
        return accepted, rejected
    refs = list({key[0] for key in proposed})
    start = min(key[3] for key in proposed)
    existing = {}
    for offset in range(0, len(refs), 500):
        for sample in db.scalars(select(EcsMetricSample).where(EcsMetricSample.resource_ref.in_(refs[offset:offset+500]),
                                EcsMetricSample.observed_at >= start)):
            existing[(sample.resource_ref, sample.metric, sample.source, sample.observed_at)] = sample
    rows = []
    for key, (row, point) in proposed.items():
        old = existing.get(key)
        if old is not None:
            if (old.tenant_id != row["tenant_id"] or old.project_ref != row["project_ref"]
                    or old.value != row["value"] or old.dimensions != row["dimensions"]):
                rejected += 1
                continue
        else:
            rows.append(row)
        accepted[key[0]].append(point)
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    for offset in range(0, len(rows), 100):
        db.execute(insert(EcsMetricSample).values(rows[offset:offset+100]).on_conflict_do_nothing(
            index_elements=["resource_ref", "metric", "source", "observed_at"]))
    return accepted, rejected


def _labels(db: Session, rows: Sequence[Resource]) -> tuple[dict[int, HcsAccount], dict[int, HcsProject]]:
    tids = {r.tenant_id for r in rows if r.tenant_id is not None}
    pids = {r.project_ref for r in rows if r.project_ref is not None}
    tenants = {t.id: t for t in db.scalars(select(HcsAccount).where(HcsAccount.id.in_(tids)))} if tids else {}
    projects = {p.id: p for p in db.scalars(select(HcsProject).where(HcsProject.id.in_(pids)))} if pids else {}
    return tenants, projects


def _stage_enabled(policy: dict, key: str) -> bool:
    return bool((policy.get(key) or {}).get("enabled", True))


def instance_out(resource: Resource, tenant: HcsAccount | None, project: HcsProject | None,
                 policy: dict, *, master: bool, task_enabled: bool = True,
                 inspection_task_enabled: bool = True,
                 now: datetime | None = None) -> EcsInstanceOut:
    now = now or utcnow()
    if project is not None and project.account_id != resource.tenant_id:
        project = None
    selected = included(resource, policy)
    active = (master and (tenant is None or tenant.enabled) and (project is None or project.enabled)
              and (resource.project_ref is None or project is not None))
    sampling = active and task_enabled and _stage_enabled(policy, "metrics")
    inspection = active and inspection_task_enabled and _stage_enabled(policy, "inspection")
    source = ecs_source(tenant, policy)
    raw_meta = (resource.attrs or {}).get("ecs_monitoring")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    signature = config_signature(tenant, policy, source)
    checked = parse_at(meta.get("checked_at"))
    current = meta.get("config_signature") == signature and meta.get("source") == source
    max_age = int(policy.get("stale_after_sec", 900))
    wanted = policy.get("metric_keys", list(METRICS))
    latest: dict[MetricName, EcsLatest] = {}
    raw_data = meta.get("latest")
    data = raw_data if isinstance(raw_data, dict) else {}
    for key in METRICS:
        raw_item = data.get(key)
        item = raw_item if (current and source != "NONE" and isinstance(raw_item, dict)
                            and raw_item.get("source") == source) else {}
        at, collected = parse_at(item.get("observed_at")), parse_at(item.get("collected_at"))
        value = number(item.get("value"))
        state = "NO_DATA"
        if not selected:
            state = "NOT_SELECTED"
        elif not sampling or key not in wanted:
            state = "DISABLED"
        elif source == "NONE":
            state = "SOURCE_UNCONFIGURED"
        elif value is not None and at is not None and collected is not None and at <= now + timedelta(seconds=30):
            state = "STALE" if at < now - timedelta(seconds=max_age) else "FRESH"
        elif current and isinstance(meta.get("diagnostic"), dict) and str(meta["diagnostic"].get("status", "")).upper() in (
                "ERROR", "PARTIAL"):
            state = "ERROR"
        else:
            value = None
        if state not in ("FRESH", "STALE"):
            value = None
        latest[cast(MetricName, key)] = EcsLatest(value=value, observed_at=at, collected_at=collected,
            source=source if source != "NONE" else None, state=state, unit="%",  # type: ignore[arg-type]
            timestamp_semantics=cast(TimestampSemantics, item.get("timestamp_semantics"))
                if item.get("timestamp_semantics") in ("source_sample", "evaluation") else "unknown")
    states = {point.state for key, point in latest.items() if key in wanted}
    if not states:
        states = {"DISABLED"}
    status = "FRESH" if states == {"FRESH"} else next((s for s in (
        "NOT_SELECTED", "DISABLED", "SOURCE_UNCONFIGURED", "MAPPING_MISSING", "ERROR", "STALE") if s in states), "NO_DATA")
    if "FRESH" in states and states != {"FRESH"}:
        status = "PARTIAL"
    query_status = str((meta.get("diagnostic") or {}).get("status", "")).upper() if isinstance(
        meta.get("diagnostic"), dict) else ""
    if current and sampling and selected and query_status in ("ERROR", "PARTIAL"):
        status = query_status
    message = DIAGNOSTICS.get(status, DIAGNOSTICS["NO_DATA"])
    if status == "SOURCE_UNCONFIGURED":
        message = REASONS[unavailable_reason(tenant)]
    elif current and sampling and selected and query_status.lower() in INCOMPLETE:
        message = diagnostic_message(meta["diagnostic"])
    semantics = {key: point.timestamp_semantics for key, point in latest.items() if key in wanted}
    observed = any(point.observed_at is not None for point in latest.values())
    if observed and "evaluation" in semantics.values():
        message += "；PromQL 时间为求值时间，非 exporter 物理采样时间；真实求值结果已保留"
    if observed and "unknown" in semantics.values():
        message += "；部分指标时间语义未知，不能证明物理采样新鲜度"
    if observed and "source_sample" in semantics.values():
        message += "；源时间按上游/显式查询返回，表达式仍须现场核实"
    diagnostic = EcsDiagnostic(status=status, message=message, at=checked, timestamp_semantics=semantics)
    return EcsInstanceOut(id=resource.id, tenant_id=resource.tenant_id, tenant_name=tenant.name if tenant else "未分配租户",
        project_ref=resource.project_ref, project_name=(project.project_name or project.project_id) if project else None,
        resource_id=resource.resource_id, name=resource.name, ip=resource.ip, status_raw=resource.status_raw,
        health=current_health(resource, now), critical=resource.critical, monitored=resource.monitored,
        scope_excluded=bool(resource.scope_excluded), included=selected,
        metrics_enabled=sampling, inspection_enabled=inspection, metrics_source=source,
        metrics_interval_sec=(policy.get("metrics") or {}).get("interval_sec", 300),
        inspection_interval_sec=(policy.get("inspection") or {}).get("interval_sec", 120), stale_after_sec=max_age,
        latest=latest, diagnostic=diagnostic)


def _resources(scope: Scope):
    return scope.apply(select(Resource).where(Resource.resource_type == "ecs_server"),
                       Resource.tenant_id)


def _outputs(db: Session, rows: Sequence[Resource], now: datetime) -> list[EcsInstanceOut]:
    tenants, projects = _labels(db, rows)
    policies = {tid: policy_dict(db, tid) for tid in {r.tenant_id for r in rows}}
    switches = collect_switch.load(db)
    task, inspect_task = db.get(InspectionTask, "ecs_metrics"), db.get(InspectionTask, "ecs_status")
    return [instance_out(row, tenants.get(row.tenant_id) if row.tenant_id is not None else None,
            projects.get(row.project_ref) if row.project_ref is not None else None, policies[row.tenant_id],
            master=switches.enabled(row.tenant_id, "ecs_server"), task_enabled=task.enabled if task else True,
            inspection_task_enabled=inspect_task.enabled if inspect_task else True, now=now)
            for row in rows]


def snapshots(db: Session, rows: Sequence[Resource]) -> dict[int, EcsInstanceOut]:
    """台账共享相同的时间、开关、来源判断；仅计算响应，不修改持久化快照。"""
    ecs = [row for row in rows if row.resource_type == "ecs_server"
           and isinstance((row.attrs or {}).get("ecs_monitoring"), dict)]
    return {item.id: item for item in _outputs(db, ecs, utcnow())} if ecs else {}


def list_instances(db: Session, scope: Scope, *, tenant_id: int | None = None, project_ref: int | None = None,
                   keyword: str | None = None, monitored: bool | None = None, critical: bool | None = None,
                   scope_excluded: bool | None = None, page: int = 1, page_size: int = 50) -> EcsInstancesOut:
    query = _resources(scope).where(Resource.lifecycle == "ACTIVE")
    if tenant_id is not None:
        scope.ensure(tenant_id, "租户")
        query = query.where(Resource.tenant_id == tenant_id)
    if project_ref is not None:
        query = query.where(Resource.project_ref == project_ref)
    if keyword and keyword.strip():
        pattern = "%" + keyword.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(or_(Resource.name.ilike(pattern, escape="\\"), Resource.resource_id.ilike(pattern, escape="\\"),
                               Resource.ip.ilike(pattern, escape="\\")))
    for column, value in ((Resource.monitored, monitored), (Resource.critical, critical),
                          (Resource.scope_excluded, scope_excluded)):
        if value is not None:
            query = query.where(column.is_(value))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(db.scalars(query.order_by(Resource.name, Resource.id).offset((page-1)*page_size).limit(page_size)))
    return EcsInstancesOut(items=_outputs(db, rows, utcnow()), total=total, page=page, page_size=page_size)


@dataclass(frozen=True)
class HistoryQuery:
    start_at: datetime
    end_at: datetime
    interval_sec: int
    metrics: tuple[MetricName, ...]

    @classmethod
    def create(cls, start_at: datetime, end_at: datetime, interval_sec: int,
               metrics: Sequence[str] | None = None) -> "HistoryQuery":
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise BizError(ErrorCode.BAD_REQUEST, "起止时间须包含时区")
        start, end = start_at.astimezone(UTC), end_at.astimezone(UTC)
        if not timedelta(0) < end - start <= timedelta(days=31):
            raise BizError(ErrorCode.BAD_REQUEST, "时间范围须大于 0 且不超过 31 天")
        if interval_sec not in (60, 300, 900, 3600) or ceil((end-start).total_seconds()/interval_sec) > 2160:
            raise BizError(ErrorCode.BAD_REQUEST, "时间粒度须为 60、300、900 或 3600 秒，且不超过 2160 个时段")
        wanted = tuple(dict.fromkeys(metrics if metrics is not None else METRICS))
        if not wanted or any(key not in METRICS for key in wanted):
            raise BizError(ErrorCode.BAD_REQUEST, "请选择 CPU、内存或磁盘使用率")
        if end > utcnow() + timedelta(seconds=60):
            raise BizError(ErrorCode.BAD_REQUEST, "结束时间不能晚于当前时间")
        return cls(start, end, interval_sec, cast(tuple[MetricName, ...], wanted))


def history(db: Session, scope: Scope, resource_ref: int, query: HistoryQuery) -> EcsHistoryOut:
    resource = db.scalar(_resources(scope).where(Resource.id == resource_ref))
    if resource is None:
        raise not_found("ECS 实例")
    now = utcnow()
    instance = _outputs(db, [resource], now)[0]
    sample = EcsMetricSample
    epoch = func.extract("epoch", sample.observed_at) if db.get_bind().dialect.name == "postgresql" else sql_cast(
        func.strftime("%s", sample.observed_at), Integer)
    delta = (epoch - query.start_at.timestamp()) / query.interval_sec
    bucket = func.floor(delta) if db.get_bind().dialect.name == "postgresql" else sql_cast(delta, Integer)
    raw_semantics = sample.dimensions["__ecs_monitoring"]["timestamp_semantics"].as_string()
    semantics = case((raw_semantics.in_(("evaluation", "source_sample")), raw_semantics), else_="unknown")
    records = db.execute(select(sample.metric, sample.source, semantics, bucket.label("bucket"), func.avg(sample.value),
            func.max(sample.value), func.min(sample.value), func.count()).where(
        sample.resource_ref == resource.id, sample.tenant_id == resource.tenant_id,
        sample.project_ref == resource.project_ref, sample.metric.in_(query.metrics),
        sample.observed_at >= query.start_at, sample.observed_at < min(query.end_at, now + timedelta(seconds=30)),
        sample.source.in_(("AOM", "PROMETHEUS", "CES"))).group_by(sample.metric, sample.source, semantics, bucket))
    groups: dict[tuple[str, str, str], dict[int, tuple]] = defaultdict(dict)
    for metric, source, meaning, index, average, maximum, minimum, count in records:
        groups[(metric, source, meaning)][int(index)] = (average, maximum, minimum, count)
    count = ceil((query.end_at-query.start_at).total_seconds()/query.interval_sec)
    series = []
    for metric in query.metrics:
        sources = sorted((source, meaning) for key, source, meaning in groups if key == metric) or [("NONE", "unknown")]
        for source, meaning in sources:
            by_bucket = groups.get((metric, source, meaning), {})
            points = []
            for index in range(count):
                values = by_bucket.get(index)
                points.append(EcsHistoryPoint(at=query.start_at + timedelta(seconds=index*query.interval_sec),
                    average=round(values[0], 4) if values else None, maximum=values[1] if values else None,
                    minimum=values[2] if values else None, sample_count=values[3] if values else 0))
            series.append(EcsHistorySeries(metric=metric, label=METRICS[metric], source=source, points=points,
                timestamp_semantics=cast(TimestampSemantics, meaning)))
    warnings = []
    if any(source == "AOM" for _, source, _ in groups):
        warnings.append("AOM 已停用；此来源仅为停用前的历史记录，不参与当前值和巡检告警")
    if not groups:
        warnings.append("所选时段没有实际采样；启用后开始积累，不使用当前值回填历史")
    if query.start_at < now - timedelta(days=RETENTION_DAYS):
        warnings.append("本地样本保留 90 天，超过保留期的时段可能已清理")
    if len({source for _, source, _ in groups}) > 1:
        warnings.append("所选时段发生过指标源切换，不同来源分开展示")
    if any(meaning == "evaluation" for _, _, meaning in groups):
        warnings.append("PromQL 求值时间只代表查询结果时刻，不证明 exporter 物理采样新鲜度；真实结果保留")
    if any(meaning == "unknown" for _, _, meaning in groups):
        warnings.append("部分历史数据时间语义未知，未按当前配置推断或回填源采样时间")
    if any(meaning == "source_sample" for _, _, meaning in groups):
        warnings.append("源时间来自上游或显式时间查询；须现场确认表达式取原始序列时间，聚合后 timestamp 可能仍为求值时间")
    return EcsHistoryOut(instance=instance, start_at=query.start_at, end_at=query.end_at,
        interval_sec=query.interval_sec, generated_at=now, warnings=warnings, series=series)
