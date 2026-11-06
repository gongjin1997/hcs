"""DCS 原生当前值与真实样本；本地查询/导出不会触发云请求。"""
from collections import defaultdict
from datetime import datetime, timedelta
from math import ceil
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.collectors.base import Observation, RunContext
from app.core.config import get_settings
from app.core.deps import Scope
from app.core.response import not_found
from app.metrics.dcs_samples import DCS_METRICS, metric_values, parse_observed_at
from app.models import Resource
from app.models.base import utcnow
from app.models.dcs import DcsMetricSample
from app.schemas.dcs import (
    DcsMemorySummary,
    DcsMetricDefinition,
    DcsMetricPoint,
    DcsMetricSeries,
    DcsMetricsHistoryOut,
    DcsMetricSummary,
    DcsNode,
    DcsTopology,
)


def native_current(resource: Resource, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    attrs = resource.attrs or {}
    meta = attrs.get("dcs_monitoring")
    meta = meta if isinstance(meta, dict) else {}
    values = metric_values(resource.metrics or {})
    observed = parse_observed_at(meta.get("observed_at"))
    threshold = meta.get("stale_after_sec", 900)
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 60:
        threshold = 900
    legacy = attrs.get("metrics_source") in ("AOM", "CES") or meta.get("source") in ("AOM", "CES")
    native = (not legacy and attrs.get("metrics_source") == "DCS" and meta.get("source") == "DCS"
              and meta.get("api_version") == "v2" and meta.get("time_basis") == "RESPONSE_RECEIVED"
              and meta.get("endpoint") in ("instances", "instances/statistic"))
    age = (now - observed).total_seconds() if observed else None
    state = "LEGACY" if legacy else "NO_DATA"
    if native and age is not None and age >= -30 and any(value is not None for value in values.values()):
        state = "FRESH" if age <= threshold else "STALE"
    if state != "FRESH":
        values = {key: None for key in DCS_METRICS}
    warning = meta.get("warning") if native and isinstance(meta.get("warning"), str) else None
    if state == "LEGACY":
        warning = "旧监控快照不作为当前原生统计，请等待 DCS 采样"
    elif state == "STALE":
        warning = "DCS 原生样本已过期，请检查采集任务"
    return {**values, "metrics_source": "DCS" if native else "NONE", "api_version": "v2" if native else None,
        "source_endpoint": meta.get("endpoint") if native else None,
        "metrics_observed_at": observed if native else None,
        "time_basis": "RESPONSE_RECEIVED" if native else None, "freshness": state,
        "sample_age_sec": max(0, age) if native and age is not None else None,
        "stale_after_sec": threshold, "warning": warning}


TOPOLOGY_STATES = {"AVAILABLE", "NOT_RETURNED", "NOT_SUPPORTED", "STALE", "UNAVAILABLE", "NOT_QUERIED"}
TOPOLOGY_COUNTS = ("node_count", "shards", "masters", "replicas", "proxies", "inactive", "inactive_masters",
                   "transitional", "unknown")
NODE_FIELDS = ("name", "node_name", "role", "kind", "status", "shard", "shard_id", "ip", "port", "az")


def topology_out(resource: Resource, now: datetime | None = None) -> DcsTopology | None:
    """本地保存的节点汇总；过期判断沿用该实例 DCS 统计的有效期，接口失败时的旧汇总一律标为过期。"""
    attrs = resource.attrs or {}
    raw = attrs.get("dcs_topology")
    if not isinstance(raw, dict) or raw.get("state") not in TOPOLOGY_STATES:
        return None
    meta = attrs.get("dcs_monitoring")
    threshold = meta.get("stale_after_sec", 900) if isinstance(meta, dict) else 900
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 60:
        threshold = 900
    observed = parse_observed_at(raw.get("observed_at"))
    age = ((now or utcnow()) - observed).total_seconds() if observed else None
    freshness = ("NO_DATA" if age is None or age < -30 else
                 "STALE" if raw["state"] == "STALE" or age > threshold else "FRESH")
    counts = {key: raw.get(key) if type(raw.get(key)) is int and raw[key] >= 0 else None for key in TOPOLOGY_COUNTS}
    warning = raw.get("warning") if isinstance(raw.get("warning"), str) else None
    return DcsTopology(state=raw["state"], observed_at=observed, freshness=freshness, warning=warning, **counts)


def nodes_out(resource: Resource) -> list[DcsNode]:
    nodes = (resource.attrs or {}).get("nodes")
    if not isinstance(nodes, list):
        return []
    return [DcsNode(**{key: node.get(key) if isinstance(node.get(key), str) else None for key in NODE_FIELDS})
            for node in nodes if isinstance(node, dict) and isinstance(node.get("name"), str) and node["name"]]


def save_metric_samples(db: Session, groups: dict[tuple[str, str], list[Observation]],
                        resources: dict[tuple[str, str], Resource], ctx: RunContext, now: datetime) -> None:
    rows: dict[tuple[int, datetime], dict] = {}
    for key, observations in groups.items():
        if key[0] != "dcs_instance":
            continue
        resource = resources[key]
        if (not resource.monitored or resource.lifecycle != "ACTIVE" or resource.tenant_id != ctx.tenant_id
                or resource.project_ref != ctx.project_ref):
            continue
        for observation in observations:
            meta = observation.attrs.get("dcs_monitoring")
            at = observation.metric_observed_at
            if (not isinstance(meta, dict) or meta.get("source") != "DCS" or meta.get("api_version") != "v2"
                    or meta.get("time_basis") != "RESPONSE_RECEIVED" or at is None or at.tzinfo is None
                    or parse_observed_at(meta.get("observed_at")) != at
                    or meta.get("endpoint") not in ("instances", "instances/statistic")):
                continue
            values = metric_values(observation.metrics)
            if not any(value is not None for value in values.values()):
                continue
            rows[(resource.id, at)] = {"resource_ref": resource.id, "tenant_id": resource.tenant_id,
                "project_ref": resource.project_ref, "observed_at": at, "collected_at": now,
                "source": "DCS", "api_version": "v2", "endpoint": meta["endpoint"], **values}
    if not rows:
        return
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    batch = list(rows.values())
    for start in range(0, len(batch), 100):
        db.execute(insert(DcsMetricSample).values(batch[start:start + 100]).on_conflict_do_nothing(
            index_elements=["resource_ref", "observed_at"]))


def metrics_history(db: Session, scope: Scope, query) -> DcsMetricsHistoryOut:
    from app.services.dcs_history_service import _labels, _resources, instance_out

    found = db.scalars(_resources(scope).where(Resource.id.in_(query.resource_ids))).all()
    by_id = {resource.id: resource for resource in found}
    if len(by_id) != len(query.resource_ids):
        raise not_found("DCS 实例")
    resources = [by_id[key] for key in query.resource_ids]
    tenants, projects = _labels(db, resources)
    raw: dict[int, list[DcsMetricSample]] = defaultdict(list)
    samples = db.scalars(select(DcsMetricSample).where(DcsMetricSample.resource_ref.in_(query.resource_ids),
        DcsMetricSample.source == "DCS", DcsMetricSample.api_version == "v2",
        DcsMetricSample.observed_at >= query.start_at, DcsMetricSample.observed_at < query.end_at)
        .order_by(DcsMetricSample.observed_at, DcsMetricSample.id))
    for point in samples:
        resource = by_id[point.resource_ref]
        if point.tenant_id == resource.tenant_id and point.project_ref == resource.project_ref:
            raw[point.resource_ref].append(point)
    seconds = query.interval_minutes * 60
    count = ceil((query.end_at - query.start_at).total_seconds() / seconds)
    series = []
    for resource in resources:
        samples_for_resource = raw[resource.id]
        buckets: dict[int, list[DcsMetricSample]] = defaultdict(list)
        for sample in samples_for_resource:
            buckets[int((sample.observed_at - query.start_at).total_seconds() // seconds)].append(sample)
        points = []
        for index in range(count):
            rows = buckets.get(index, [])
            values: dict[str, Any] = {}
            counts: dict[str, int] = {}
            for key, (_, _, aggregation) in DCS_METRICS.items():
                valid = [(row.observed_at, getattr(row, key)) for row in rows if getattr(row, key) is not None]
                counts[key] = len(valid)
                values[key] = (sum(value for _, value in valid) / len(valid)
                               if aggregation == "mean" else valid[-1][1]) if valid else None
            peaks = [row.memory_usage for row in rows if row.memory_usage is not None]
            points.append(DcsMetricPoint(at=query.start_at + timedelta(seconds=index * seconds), **values,
                memory_max=max(peaks) if peaks else None, sample_count=len(rows), metric_sample_counts=counts,
                last_observed_at=rows[-1].observed_at if rows else None))
        summaries = {}
        for key, (_, _, aggregation) in DCS_METRICS.items():
            valid_samples = [row for row in samples_for_resource if getattr(row, key) is not None]
            summary_values = [getattr(row, key) for row in valid_samples]
            summaries[key] = DcsMetricSummary(
                average=sum(summary_values) / len(summary_values) if summary_values and aggregation == "mean" else None,
                peak=max(summary_values) if summary_values else None, latest=summary_values[-1] if summary_values else None,
                latest_at=valid_samples[-1].observed_at if valid_samples else None, sample_count=len(valid_samples))
        memory = summaries["memory_usage"]
        present = sum(point.sample_count > 0 for point in points)
        complete = all(all(point.metric_sample_counts.values()) for point in points)
        instance = instance_out(resource, tenants, projects)
        warning = None if complete else "部分指标或时段未采集，缺失值留空" if present else "所选时段暂无原生采样"
        series.append(DcsMetricSeries(instance=instance, source="LOCAL" if present else "NONE",
            status="AVAILABLE" if complete else "PARTIAL" if present else "NO_DATA", warning=warning,
            valid_points=present, total_points=count, points=points, summaries=summaries,
            summary=DcsMemorySummary(average_usage=memory.average, peak_usage=memory.peak,
                                     latest_usage=memory.latest, latest_at=memory.latest_at)))
    settings = get_settings()
    return DcsMetricsHistoryOut(start_at=query.start_at, end_at=query.end_at, interval_minutes=query.interval_minutes,
        timezone=settings.timezone, demo=settings.env != "prod" and bool(settings.hcs_mock_url), generated_at=utcnow(),
        metrics=[DcsMetricDefinition(key=key, label=label, unit=unit, aggregation=aggregation)
                 for key, (label, unit, aggregation) in DCS_METRICS.items()], series=series)
