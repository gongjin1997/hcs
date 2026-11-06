"""GaussDB 本地原生存储样本：同一采集事务写入，查询不访问云。"""
from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.collectors.base import Observation, RunContext
from app.collectors.gaussdb import GaussStorageObservation
from app.core.deps import Scope
from app.core.response import BizError, ErrorCode
from app.hcs.gaussdb_native import storage_percent
from app.metrics.dcs_samples import finite_number
from app.models import Resource
from app.models.base import utcnow
from app.models.gaussdb import GaussDBStorageSample
from app.schemas.gaussdb import GaussDiskHistoryOut, GaussDiskHistoryPoint

MAX_POINTS = 5000


def persist_disk_samples(db: Session, groups: dict[tuple[str, str], list[Observation]],
                         resources: dict[tuple[str, str], Resource], ctx: RunContext, collected_at: datetime) -> int:
    """资源 flush 后调用；不 commit。时间来自 collector 接收响应时刻，而非源端采样时刻或 pipeline 的 now。"""
    from app.services.collect_switch import load

    if (ctx.tenant_id is None or ctx.project_ref is None
            or not load(db).enabled(ctx.tenant_id, "gaussdb_instance")):
        return 0
    samples = []
    for key, observations in groups.items():
        row = resources.get(key)
        if (key[0] != "gaussdb_instance" or row is None or row.id is None or not row.monitored
                or row.lifecycle != "ACTIVE" or row.tenant_id != ctx.tenant_id or row.project_ref != ctx.project_ref):
            continue
        candidates = [(o.metric_observed_at, index, o) for index, o in enumerate(observations)
                      if isinstance(o, GaussStorageObservation) and o.check_item in ("instance_status", "disk")
                      and o.metric_observed_at is not None and o.metric_observed_at.tzinfo is not None
                      and o.storage_status in ("AVAILABLE", "PARTIAL", "ERROR", "UNAVAILABLE")]
        if not candidates:
            continue
        # 同毫秒的状态清空观测在前、真实磁盘观测在后；时间相同时以后者为准。
        observed_at, _, observation = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))
        status = observation.storage_status
        used = finite_number(observation.metrics.get("disk_used_gb"))
        total = finite_number(observation.metrics.get("disk_total_gb"))
        if status in ("ERROR", "UNAVAILABLE") or used is None or total is None:
            used = total = usage = None
            status = status if status in ("ERROR", "UNAVAILABLE") else "ERROR"
        else:
            usage = storage_percent(used, total)
            if total > 0 and usage is None:
                used = total = None
                status = "ERROR"
            else:
                status = "AVAILABLE" if usage is not None else "PARTIAL"
        samples.append({"resource_ref": row.id, "tenant_id": row.tenant_id, "project_ref": row.project_ref,
                        "observed_at": observed_at, "collected_at": observed_at,
                        "status": status, "used_gb": used, "total_gb": total, "usage_percent": usage})
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    for start in range(0, len(samples), 100):
        db.execute(insert(GaussDBStorageSample).values(samples[start:start + 100]).on_conflict_do_nothing(
            index_elements=["resource_ref", "observed_at"]))
    return len(samples)


def disk_history(db: Session, scope: Scope, resource_ref: int, *, start_at: datetime, end_at: datetime,
                 period_sec: int = 300) -> GaussDiskHistoryOut:
    from app.services.gaussdb_monitoring import _target, instance_out, window

    target = _target(db, scope, resource_ref)
    start, end = window(start_at, end_at)
    if isinstance(period_sec, bool) or not isinstance(period_sec, int) or not 60 <= period_sec <= 86400:
        raise BizError(ErrorCode.BAD_REQUEST, "采样间隔须为 60–86400 秒的整数")
    count = ceil((end - start).total_seconds() / period_sec)
    if count > MAX_POINTS:
        raise BizError(ErrorCode.BAD_REQUEST, "历史查询最多 5000 个时间桶，请增大采样间隔或缩小时间范围")
    points = [GaussDiskHistoryPoint(timestamp=start + timedelta(seconds=index * period_sec)) for index in range(count)]
    row = target.resource
    stmt = select(GaussDBStorageSample).where(
        GaussDBStorageSample.resource_ref == row.id, GaussDBStorageSample.tenant_id == row.tenant_id,
        GaussDBStorageSample.project_ref == row.project_ref, GaussDBStorageSample.observed_at >= start,
        GaussDBStorageSample.observed_at < end).order_by(GaussDBStorageSample.observed_at, GaussDBStorageSample.id)
    for sample in db.scalars(stmt.execution_options(yield_per=1000)):
        index = int((sample.observed_at - start).total_seconds() // period_sec)
        point = GaussDiskHistoryPoint(timestamp=points[index].timestamp)
        if sample.status in ("AVAILABLE", "PARTIAL"):
            used, total = finite_number(sample.used_gb), finite_number(sample.total_gb)
            usage = storage_percent(used, total) if used is not None and total is not None else None
            if used is not None and total is not None and (total == 0 or usage is not None):
                point.observed_at = sample.observed_at
                point.collected_at = sample.collected_at
                point.used_gb, point.total_gb = used, total
                point.usage_percent = usage
        points[index] = point
    available = sum(p.observed_at is not None for p in points)
    complete = sum(p.usage_percent is not None for p in points)
    return GaussDiskHistoryOut(instance=instance_out(target),
        status="NO_DATA" if not available else "AVAILABLE" if complete == count else "PARTIAL",
        queried_at=utcnow(), start_at=start, end_at=end, period_sec=period_sec, points=points,
        warning=("本地定时采样历史；缺失或失败处保持空值，不使用当前快照补齐" if complete != count else None))
