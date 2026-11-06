"""DCS 原生接口实际采样历史；查询与导出不调用外部指标服务。"""
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Literal, cast

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import Scope
from app.core.response import BizError, ErrorCode, not_found
from app.metrics.dcs_samples import RawMemoryPoint, finite_number
from app.models import HcsAccount, HcsProject, Resource
from app.models.base import utcnow
from app.models.dcs import DcsMemorySample
from app.schemas.dcs import (
    DcsHistoryOut,
    DcsInstanceOut,
    DcsInstancesOut,
    DcsMemoryPoint,
    DcsMemorySeries,
    DcsMemorySummary,
    IntervalMinutes,
)
from app.services.dcs_monitoring import native_current


@dataclass(frozen=True)
class MemoryQuery:
    resource_ids: tuple[int, ...]
    start_at: datetime
    end_at: datetime
    interval_minutes: IntervalMinutes

    @classmethod
    def create(cls, resource_ids: Sequence[int], start_at: datetime, end_at: datetime,
               interval_minutes: int) -> "MemoryQuery":
        if not resource_ids or len(resource_ids) > 20 or any(i < 1 for i in resource_ids):
            raise BizError(ErrorCode.BAD_REQUEST, "请选择 1–20 个实例")
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise BizError(ErrorCode.BAD_REQUEST, "起止时间须包含时区")
        start, end = start_at.astimezone(UTC), end_at.astimezone(UTC)
        if end <= start or end - start > timedelta(days=31):
            raise BizError(ErrorCode.BAD_REQUEST, "时间范围须大于 0 且不超过 31 天")
        if interval_minutes not in (5, 10, 15, 30, 60):
            raise BizError(ErrorCode.BAD_REQUEST, "时间粒度须为 5、10、15、30 或 60 分钟")
        return cls(tuple(dict.fromkeys(resource_ids)), start, end, cast(IntervalMinutes, interval_minutes))


def _resources(scope: Scope):
    return scope.apply(select(Resource).where(Resource.resource_type == "dcs_instance", Resource.lifecycle == "ACTIVE"),
                       Resource.tenant_id)


def _labels(db: Session, rows: Sequence[Resource]) -> tuple[dict[int, HcsAccount], dict[int, HcsProject]]:
    tids = {r.tenant_id for r in rows if r.tenant_id is not None}
    pids = {r.project_ref for r in rows if r.project_ref is not None}
    tenants = {t.id: t for t in db.scalars(select(HcsAccount).where(HcsAccount.id.in_(tids)))} if tids else {}
    projects = {p.id: p for p in db.scalars(select(HcsProject).where(HcsProject.id.in_(pids)))} if pids else {}
    return tenants, projects


def instance_out(row: Resource, tenants: dict[int, HcsAccount], projects: dict[int, HcsProject]) -> DcsInstanceOut:
    tenant = tenants.get(row.tenant_id) if row.tenant_id is not None else None
    project = projects.get(row.project_ref) if row.project_ref is not None else None
    if project and project.account_id != row.tenant_id:
        project = None
    metrics = native_current(row)
    cpu = finite_number((row.metrics or {}).get("cpu_usage")) if metrics.get("freshness") == "FRESH" else None
    return DcsInstanceOut(id=row.id, resource_id=row.resource_id, name=row.name, cpu_usage=cpu,
                          scope_excluded=bool(row.scope_excluded),
                          tenant_id=row.tenant_id, tenant_name=tenant.name if tenant else "未分配租户",
                          project_ref=row.project_ref,
                          project_name=(project.project_name or project.project_id) if project else None,
                          region=row.region, status_raw=row.status_raw,
                          snapshot_at=row.last_seen, **metrics)


def get_instance(db: Session, scope: Scope, resource_ref: int) -> DcsInstanceOut:
    row = db.scalar(_resources(scope).where(Resource.id == resource_ref))
    if row is None:
        raise not_found("DCS 实例")
    tenants, projects = _labels(db, [row])
    return instance_out(row, tenants, projects)


def list_instances(db: Session, scope: Scope, *, keyword: str | None = None, tenant_id: int | None = None,
                   project_ref: int | None = None, page: int = 1, page_size: int = 100) -> DcsInstancesOut:
    query = _resources(scope)
    if tenant_id is not None:
        scope.ensure(tenant_id, "租户")
        query = query.where(Resource.tenant_id == tenant_id)
    if project_ref is not None:
        query = query.where(Resource.project_ref == project_ref)
    if keyword and keyword.strip():
        pattern = f"%{keyword.strip()}%"
        query = query.where(or_(Resource.name.ilike(pattern), Resource.resource_id.ilike(pattern)))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(Resource.name, Resource.id).offset((page - 1) * page_size).limit(page_size)).all()
    tenants, projects = _labels(db, rows)
    return DcsInstancesOut(items=[instance_out(r, tenants, projects) for r in rows],
                           total=total, page=page, page_size=page_size)


def aggregate(instance: DcsInstanceOut, raw: list[RawMemoryPoint], query: MemoryQuery,
              source: Literal["LOCAL", "NONE"], warning: str | None) -> DcsMemorySeries:
    seconds = query.interval_minutes * 60
    count = ceil((query.end_at - query.start_at).total_seconds() / seconds)
    buckets: dict[int, list[RawMemoryPoint]] = defaultdict(list)
    valid: list[RawMemoryPoint] = []
    for point in raw:
        if query.start_at <= point.at < query.end_at:
            index = int((point.at - query.start_at).total_seconds() // seconds)
            buckets[index].append(point)
            if point.memory_usage is not None:
                valid.append(point)
    points = []
    for index in range(count):
        rows = buckets.get(index, [])
        values = [p.memory_usage for p in rows if p.memory_usage is not None]
        peaks = [p.memory_max for p in rows if p.memory_max is not None]
        used = [p.used_memory_mb for p in rows if p.used_memory_mb is not None]
        capacity = [p for p in rows if p.max_memory_mb is not None]
        last_capacity = max(capacity, key=lambda p: p.at).max_memory_mb if capacity else None
        points.append(DcsMemoryPoint(at=query.start_at + timedelta(seconds=index * seconds),
                                     memory_usage=sum(values) / len(values) if values else None,
                                     memory_max=max(peaks) if peaks else None,
                                     used_memory_mb=sum(used) / len(used) if used else None,
                                     max_memory_mb=last_capacity, sample_count=len(values)))
    latest = max(valid, key=lambda p: p.at) if valid else None
    peaks = [p.memory_max for p in raw if query.start_at <= p.at < query.end_at and p.memory_max is not None]
    valid_values = [p.memory_usage for p in valid if p.memory_usage is not None]
    summary = DcsMemorySummary(average_usage=sum(valid_values) / len(valid_values) if valid_values else None,
                               peak_usage=max(peaks) if peaks else None,
                               latest_usage=latest.memory_usage if latest else None, latest_at=latest.at if latest else None)
    present = sum(p.memory_usage is not None for p in points)
    return DcsMemorySeries(instance=instance, source=source,
                            status="AVAILABLE" if present == count else "PARTIAL" if present else "NO_DATA",
                            warning=warning, valid_points=present, total_points=count, summary=summary, points=points)


def memory_history(db: Session, scope: Scope, query: MemoryQuery) -> DcsHistoryOut:
    found = db.scalars(_resources(scope).where(Resource.id.in_(query.resource_ids))).all()
    by_id = {r.id: r for r in found}
    if len(by_id) != len(query.resource_ids):
        raise not_found("DCS 实例")
    rows = [by_id[i] for i in query.resource_ids]
    tenants, projects = _labels(db, rows)
    local: dict[int, list[RawMemoryPoint]] = defaultdict(list)
    samples = db.scalars(select(DcsMemorySample).where(DcsMemorySample.resource_ref.in_(query.resource_ids),
                         DcsMemorySample.observed_at >= query.start_at, DcsMemorySample.observed_at < query.end_at)
                         .order_by(DcsMemorySample.observed_at))
    for point in samples:
        resource = by_id[point.resource_ref]
        if point.tenant_id != resource.tenant_id or point.project_ref != resource.project_ref:
            continue
        value = finite_number(point.memory_usage)
        if value is not None:
            local[point.resource_ref].append(RawMemoryPoint(point.observed_at, value, value,
                                             finite_number(point.used_memory_mb), finite_number(point.max_memory_mb)))
    series = []
    for resource in rows:
        tenant = tenants.get(resource.tenant_id) if resource.tenant_id is not None else None
        project = projects.get(resource.project_ref) if resource.project_ref is not None else None
        raw = local.get(resource.id, [])
        warning = None
        if not tenant or not project or project.account_id != resource.tenant_id:
            warning = "项目未关联，仅显示本地采集记录"
        elif not tenant.enabled or not project.enabled:
            warning = "租户或项目已停用，仅显示已有 DCS 接口采样记录"
        elif not raw:
            warning = "所选时段暂无 DCS 接口采样，请检查采集开关与任务记录"
        source: Literal["LOCAL", "NONE"] = "LOCAL" if raw else "NONE"
        series.append(aggregate(instance_out(resource, tenants, projects), raw, query, source, warning))
    settings = get_settings()
    return DcsHistoryOut(start_at=query.start_at, end_at=query.end_at, interval_minutes=query.interval_minutes,
                         timezone=settings.timezone, demo=settings.env != "prod" and bool(settings.hcs_mock_url),
                         generated_at=utcnow(), series=series)
