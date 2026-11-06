"""Read-only ECS metrics with upstream result times, never local collection times.

The caller owns persistence, freshness policy, and detector facts. AOM is disabled
for every resource, including tenants with old endpoint or mapping settings.
Prometheus instant vectors have period_sec=0 unless explicitly configured.
Their result timestamps are evaluation times unless explicit timestamp_queries
return matching source sample times; diagnostics state this distinction.
Only the existing, doubly opted-in legacy CES protocol is supported here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any

import httpx

from app.hcs import HcsError
from app.hcs.pager import response_items
from app.hcs.ratelimit import request_budget
from app.hcs.readonly import ces_access
from app.metrics import effective, mark, use_ces
from app.metrics.ces import BATCH, ces_list_metrics, metric_key
from app.metrics.ecs_diagnostics import unavailable_reason
from app.metrics.prometheus import DEFAULT_QUERIES, PromClient, PromError, host_of, prom_for

if TYPE_CHECKING:
    from app.collectors.base import RunContext

METRICS = ("cpu_util", "mem_util", "disk_util", "boot_time")
_PERCENT = frozenset(("%", "percent", "percentage", "百分比"))
_SECONDS = frozenset(("s", "sec", "second", "seconds"))
_FAILURES = (HcsError, PromError, httpx.HTTPError, ValueError, TypeError, KeyError)


@dataclass(frozen=True)
class EcsReading:
    metric: str
    value: float
    observed_at: datetime
    source: str
    unit: str = "%"
    dimensions: dict[str, str] = field(default_factory=dict)
    period_sec: int = 0


@dataclass
class EcsReadingsResult:
    readings: dict[str, list[EcsReading]]
    source: str
    diagnostic: dict[str, Any]


def ecs_source(tenant: object, config: dict | None, client: object | None = None) -> str:
    """AOM configuration never participates in ECS selection; no implicit fallback."""
    return effective(tenant, client=client)


def _now() -> datetime:
    return datetime.now(UTC)


def _number(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None
    try:
        value = float(raw)
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _timestamp(raw: object, *, milliseconds: bool = False) -> datetime | None:
    value = _number(raw)
    if value is None or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000 if milliseconds else value, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _units(alias: str, *values: object) -> str | None:
    expected = _SECONDS if alias == "boot_time" else _PERCENT
    evidence = [value for value in values if value is not None]
    if not evidence or any(not isinstance(value, str) or value.strip().lower() not in expected for value in evidence):
        return None
    return "s" if alias == "boot_time" else "%"


def _dimensions(raw: object) -> dict[str, str] | None:
    if not isinstance(raw, list) or not raw or len(raw) > 32:
        return None
    out: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            return None
        name, value = item.get("name"), item.get("value")
        if (not isinstance(name, str) or not name or name in out or not isinstance(value, str)
                or not value or len(name) > 255 or len(value) > 1024):
            return None
        out[name] = value
    return out


def _error(ctx: RunContext, entry: dict, source: str, alias: str, reason: str = "query_failed") -> None:
    # Never propagate exception strings: HTTP errors/config validation can contain
    # credentials, endpoint URLs, PromQL, response bodies, or sample values.
    entry["errors"] += 1
    entry["reason"] = reason
    message = f"ECS {source} {alias}: {reason}; only actual returned samples retained"
    if message not in ctx.errors:
        ctx.errors.append(message)


def _reject(entry: dict, reason: str) -> None:
    entry["rejected_points"] += 1
    entry["rejections"][reason] = entry["rejections"].get(reason, 0) + 1


def _accept(out: dict[str, list[EcsReading]], sid: str, alias: str, raw: object, at: object,
            source: str, unit: str | None, dimensions: dict[str, str], period: int,
            entry: dict, *, start: datetime | None = None, end: datetime | None = None) -> None:
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        _reject(entry, "invalid_timestamp")
        return
    at = at.astimezone(UTC)
    if at.timestamp() <= 0 or at > _now():
        _reject(entry, "invalid_timestamp")
        return
    if start is not None and at < start or end is not None and at > end:
        _reject(entry, "outside_window")
        return
    if unit is None:
        _reject(entry, "invalid_unit")
        return
    value = _number(raw)
    if value is None or (value > at.timestamp() if alias == "boot_time" else value > 100):
        _reject(entry, "invalid_value")
        return
    out.setdefault(sid, []).append(EcsReading(alias, value, at, source, unit, dict(dimensions), period))


def _ip_index(index: dict[str, str], ids: list[str]) -> dict[str, str]:
    wanted, ambiguous = set(ids), set()
    out: dict[str, str] = {}
    for raw, sid in index.items():
        if not isinstance(raw, str) or not isinstance(sid, str):
            continue
        try:
            host = str(ip_address(raw))
        except ValueError:
            continue
        if host in out and out[host] != sid:
            ambiguous.add(host)
        out[host] = sid
    return {host: sid for host, sid in out.items() if host not in ambiguous and sid in wanted}


def _prometheus(ctx: RunContext, ids: list[str], index: dict[str, str], aliases: list[str], config: dict,
                diagnostic: dict, out: dict[str, list[EcsReading]]) -> None:
    try:
        prom: PromClient | None = prom_for(ctx)
    except (*_FAILURES, AttributeError):
        for alias, entry in diagnostic["metrics"].items():
            _error(ctx, entry, "PROMETHEUS", alias, "invalid_prometheus_source")
        return
    if prom is None:
        for entry in diagnostic["metrics"].values():
            entry.update(status="no_source", reason="prometheus_not_configured")
        return
    raw = config.get("prometheus") or {}
    if not isinstance(raw, dict):
        for alias, entry in diagnostic["metrics"].items():
            _error(ctx, entry, "PROMETHEUS", alias, "invalid_prometheus_mapping")
        return
    queries = raw.get("queries", raw)
    label = raw.get("instance_label", raw.get("label", "instance"))
    period = config.get("period_sec", raw.get("period_sec", 0))
    label_filters = raw.get("label_filters", {})
    units, time_queries = raw.get("units", {}), raw.get("timestamp_queries", {})
    if (not isinstance(queries, dict) or not isinstance(label, str) or not label
            or not isinstance(units, dict) or not isinstance(time_queries, dict)
            or not isinstance(label_filters, dict)
            or any(not isinstance(key, str) or not key or not isinstance(value, str) or not value
                   for key, value in label_filters.items())
            or isinstance(period, bool) or not isinstance(period, int) or not 0 <= period <= 86400):
        for alias, entry in diagnostic["metrics"].items():
            _error(ctx, entry, "PROMETHEUS", alias, "invalid_prometheus_mapping")
        return
    index = _ip_index(index, ids)
    if not index:
        for entry in diagnostic["metrics"].values():
            entry.update(status="no_series", reason="no_unambiguous_ip")
        return
    prom.evaluation_time = _now().timestamp()
    for alias in aliases:
        entry = diagnostic["metrics"][alias]
        query = queries.get(alias, DEFAULT_QUERIES[alias])
        if not isinstance(query, str) or not query.strip():
            entry.update(status="mapping_missing", reason="metric_query_missing")
            continue
        default_unit = "s" if alias == "boot_time" else "%"
        unit = _units(alias, units.get(alias, default_unit if query == DEFAULT_QUERIES[alias] else None))
        if unit is None:
            _error(ctx, entry, "PROMETHEUS", alias, "unverified_query_unit")
            continue
        time_query = time_queries.get(alias)
        if alias in time_queries and (not isinstance(time_query, str) or not time_query.strip()):
            _error(ctx, entry, "PROMETHEUS", alias, "invalid_timestamp_query")
            continue
        entry["timestamp_semantics"] = "source_sample" if time_query else "evaluation"
        try:
            rows = prom.query(query)
            source_times: dict[tuple, datetime | None] = {}
            if time_query:
                for stamp in prom.query(time_query):
                    if (not isinstance(stamp, dict) or not isinstance(stamp.get("metric"), dict)
                            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in stamp["metric"].items())):
                        _reject(entry, "invalid_timestamp_series")
                        continue
                    key = tuple(sorted((k, v) for k, v in stamp["metric"].items() if k != "__name__"))
                    value = stamp.get("value")
                    at = _timestamp(value[1]) if isinstance(value, list | tuple) and len(value) == 2 else None
                    evaluated = _timestamp(value[0]) if isinstance(value, list | tuple) and len(value) == 2 else None
                    if evaluated is None or evaluated > _now():
                        at = None
                        _reject(entry, "invalid_evaluation_timestamp")
                    if key in source_times and source_times[key] != at:
                        source_times[key] = None
                    else:
                        source_times[key] = at
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("metric"), dict):
                    _reject(entry, "invalid_series")
                    continue
                labels = row["metric"]
                if any(not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()):
                    _reject(entry, "invalid_dimensions")
                    continue
                if any(labels.get(key) != value for key, value in label_filters.items()):
                    _reject(entry, "foreign_dimensions")
                    continue
                if ("project_id" in labels and "project_id" not in label_filters
                        and labels["project_id"] != ctx.project_id):
                    _reject(entry, "foreign_project")
                    continue
                try:
                    sid = index.get(str(ip_address(host_of(labels, label))))
                except ValueError:
                    continue
                if sid is None:
                    continue
                entry["series_count"] += 1
                sample = row.get("value")
                if not isinstance(sample, (list, tuple)) or len(sample) != 2:
                    _reject(entry, "invalid_sample")
                    continue
                evaluated = _timestamp(sample[0])
                if evaluated is None or evaluated > _now():
                    _reject(entry, "invalid_evaluation_timestamp")
                    continue
                key = tuple(sorted((k, v) for k, v in labels.items() if k != "__name__"))
                at = source_times.get(key) if time_query else evaluated
                _accept(out, sid, alias, sample[1], at, "PROMETHEUS", unit, labels, period, entry)
        except _FAILURES:
            _error(ctx, entry, "PROMETHEUS", alias)
        if not entry["series_count"] and not entry["errors"]:
            entry.update(status="no_series", reason="no_matching_series")


def _ces(ctx: RunContext, ids: list[str], aliases: list[str], config: dict, diagnostic: dict,
         out: dict[str, list[EcsReading]]) -> None:
    if not use_ces(ctx.tenant):
        return
    names = {"cpu_util": ("SYS.ECS", "cpu_util"), "mem_util": ("AGT.ECS", "mem_usedPercent"),
             "disk_util": ("AGT.ECS", "disk_usedPercent")}
    window = config.get("window_sec", 600)
    if isinstance(window, bool) or not isinstance(window, int) or not 60 <= window <= 86400:
        for alias, entry in diagnostic["metrics"].items():
            _error(ctx, entry, "CES", alias, "invalid_query_window")
        return
    end = _now()
    start = end - timedelta(seconds=window)
    diagnostic.update(window_start=start.isoformat(), window_end=end.isoformat())
    for alias in aliases:
        entry = diagnostic["metrics"][alias]
        if alias not in names:
            entry.update(status="mapping_missing", reason="legacy_metric_unsupported")
            continue
        namespace, name = names[alias]
        try:
            if alias == "disk_util":
                catalogue = ces_list_metrics(ctx.client, namespace, name)
                descriptors = []
                for row in catalogue:
                    if not isinstance(row, dict):
                        continue
                    dims = _dimensions(row.get("dimensions"))
                    if (row.get("namespace") != namespace or row.get("metric_name") != name
                            or dims is None or dims.get("instance_id") not in ids):
                        continue
                    descriptors.append({"namespace": namespace, "metric_name": name, "dimensions": row["dimensions"]})
            else:
                descriptors = [{"namespace": namespace, "metric_name": name,
                                "dimensions": [{"name": "instance_id", "value": sid}]} for sid in ids]
            allowed = {metric_key(desc): desc for desc in descriptors}
            entry["series_count"] = len(allowed)
            if not allowed:
                entry.update(status="no_series", reason="no_matching_series")
            if len(allowed) > BATCH * 20:
                raise HcsError(0, "LOCAL.REQUEST_BUDGET", "CES descriptor budget exceeded")
            descriptors = list(allowed.values())
            for offset in range(0, len(descriptors), BATCH):
                batch = descriptors[offset:offset + BATCH]
                batch_keys = {metric_key(desc) for desc in batch}
                data = ctx.client.post("ces", "/V1.0/{project_id}/batch-query-metric-data", json={
                    "metrics": batch, "from": int(start.timestamp() * 1000), "to": int(end.timestamp() * 1000),
                    "period": "1", "filter": "average"})
                for row in response_items(data, "metrics"):
                    if not isinstance(row, dict):
                        _reject(entry, "invalid_series")
                        continue
                    dims = _dimensions(row.get("dimensions"))
                    if dims is None:
                        _reject(entry, "invalid_dimensions")
                        continue
                    try:
                        key = metric_key(row)
                    except HcsError:
                        _reject(entry, "invalid_descriptor")
                        continue
                    if key not in batch_keys:
                        _reject(entry, "foreign_descriptor")
                        continue
                    points = row.get("datapoints")
                    if not isinstance(points, list):
                        _reject(entry, "invalid_sample")
                        continue
                    for point in points:
                        if not isinstance(point, dict):
                            _reject(entry, "invalid_sample")
                            continue
                        # CES compatibility queries explicitly request percent
                        # metrics. A returned conflicting unit is still rejected.
                        unit = _units(alias, point.get("unit"), row.get("unit"), "%")
                        _accept(out, dims["instance_id"], alias, point.get("average"),
                                _timestamp(point.get("timestamp"), milliseconds=True), "CES", unit,
                                dims, 1, entry, start=start, end=end)
        except _FAILURES:
            _error(ctx, entry, "CES", alias)


def _dimension_order(reading: EcsReading) -> tuple:
    dims = reading.dimensions
    return (dims.get("mountpoint", dims.get("mount_point", "")), tuple(sorted(dims.items())))


def _deduplicate(out: dict[str, list[EcsReading]], diagnostic: dict) -> dict[str, list[EcsReading]]:
    result = {}
    for sid, readings in out.items():
        ambiguous = set()
        for alias in {reading.metric for reading in readings}:
            varying = {"__name__", "mountpoint", "mount_point", "device", "fstype"} if alias == "disk_util" else {"__name__"}
            scopes = {tuple(sorted((key, value) for key, value in reading.dimensions.items() if key not in varying))
                      for reading in readings if reading.metric == alias}
            if len(scopes) > 1:
                ambiguous.add(alias)
                _reject(diagnostic["metrics"][alias], "conflicting_series")
        points: dict[tuple, list[EcsReading]] = {}
        for reading in readings:
            if reading.metric in ambiguous:
                continue
            points.setdefault((reading.metric, reading.observed_at), []).append(reading)
        accepted = []
        for (alias, _), candidates in points.items():
            entry = diagnostic["metrics"][alias]
            by_dimension: dict[tuple, list[EcsReading]] = {}
            for candidate in candidates:
                by_dimension.setdefault(tuple(sorted(candidate.dimensions.items())), []).append(candidate)
            clean = []
            for duplicates in by_dimension.values():
                if len({(point.value, point.unit, point.period_sec) for point in duplicates}) > 1:
                    _reject(entry, "conflicting_samples")
                else:
                    clean.append(duplicates[0])
            if not clean:
                continue
            if alias != "disk_util" and len({point.value for point in clean}) > 1:
                _reject(entry, "conflicting_series")
                continue
            # Maximum applies only within one actual timestamp. Ties choose a
            # stable mount/dimension ordering, independent of upstream row order.
            accepted.append(min(clean, key=lambda point: (-point.value, _dimension_order(point))))
        if accepted:
            result[sid] = sorted(accepted, key=lambda point: (point.observed_at, METRICS.index(point.metric)))
    return result


def _finish(ctx: RunContext, out: dict[str, list[EcsReading]], source: str, diagnostic: dict,
            ids: list[str]) -> EcsReadingsResult:
    out = _deduplicate(out, diagnostic)
    now = _now()
    latest: dict[str, dict[str, str]] = {}
    pairs = set()
    fresh_pairs = set()
    for sid, readings in out.items():
        for reading in readings:
            entry = diagnostic["metrics"][reading.metric]
            entry["point_count"] += 1
            age = (now - reading.observed_at).total_seconds()
            if age <= diagnostic["stale_after_sec"]:
                fresh_pairs.add((sid, reading.metric))
            entry["fresh_points" if age <= diagnostic["stale_after_sec"] else "stale_points"] += 1
            stamp = reading.observed_at.isoformat()
            if entry["latest_at"] is None or stamp > entry["latest_at"]:
                entry["latest_at"] = stamp
            previous = latest.setdefault(sid, {}).get(reading.metric)
            if previous is None or stamp > previous:
                latest[sid][reading.metric] = stamp
            pairs.add((sid, reading.metric))
    for alias, entry in diagnostic["metrics"].items():
        entry["resource_count"] = sum(metric == alias for _, metric in pairs)
        if entry["point_count"]:
            fresh_resources = sum(metric == alias for _, metric in fresh_pairs)
            incomplete = entry["errors"] or entry["rejected_points"] or fresh_resources < len(ids)
            entry["status"] = "partial" if incomplete else "ready"
            if fresh_resources < len(ids):
                entry["reason"] = "stale_samples" if not fresh_resources else "incomplete_fresh_coverage"
        elif entry["errors"]:
            entry["status"] = "error"
        if entry["rejected_points"]:
            message = f"ECS {source} {alias}: invalid or ambiguous samples discarded"
            if message not in ctx.errors:
                ctx.errors.append(message)
    entries = list(diagnostic["metrics"].values())
    statuses = {entry["status"] for entry in entries}
    if not entries:
        status = "no_data"
    elif statuses == {"ready"}:
        status = "ready"
    elif "ready" in statuses or "partial" in statuses:
        status = "partial"
    elif "error" in statuses:
        status = "error"
    else:
        status = next((value for value in ("no_source", "mapping_missing", "no_series", "no_data")
                       if value in statuses), "no_data")
    diagnostic.update(status=status, covered_resources=len(out), covered_pairs=len(pairs),
                      coverage=len(pairs) / diagnostic["requested_pairs"] if diagnostic["requested_pairs"] else 0,
                      point_count=sum(entry["point_count"] for entry in entries), latest_by_resource=latest,
                      latest_at=max((entry["latest_at"] for entry in entries if entry["latest_at"]), default=None))
    # Capability evidence is recorded only for successful, valid query results.
    if status == "ready" and source != "NONE":
        mark(ctx.tenant, source.lower(), True, "ECS metric query succeeded",
             project_ref=ctx.project_ref, project_id=ctx.project_id)
    return EcsReadingsResult(out, source, diagnostic)


def collect_readings(ctx: RunContext, ids: list[str], ip_index: dict[str, str], *, metrics: list[str],
                     config: dict | None = None) -> EcsReadingsResult:
    """Return scoped, timestamped readings plus safe per-metric diagnostics.

    Explicit config keys override ctx.params. Legacy AOM keys are ignored.
    Unrelated policy keys are ignored. No fallback source, detector facts,
    historical interpolation, endpoint guessing, or adapter-level retry occurs.
    """
    config = {**ctx.params, **(config or {})}
    ids = list(dict.fromkeys(sid for sid in ids if isinstance(sid, str) and sid))
    aliases = list(dict.fromkeys(alias for alias in metrics if alias in METRICS))
    stale = config.get("stale_after_sec", 900)
    if isinstance(stale, bool) or not isinstance(stale, (int, float)) or not math.isfinite(stale) or stale <= 0:
        stale = 900
    source = ecs_source(ctx.tenant, config, client=ctx.client)
    diagnostic = {"status": "no_data", "requested_resources": len(ids), "requested_pairs": len(ids) * len(aliases),
                  "stale_after_sec": stale, "metrics": {alias: {
                      "status": "no_data", "reason": None, "series_count": 0, "point_count": 0,
                      "resource_count": 0, "fresh_points": 0, "stale_points": 0, "latest_at": None,
                      "errors": 0, "rejected_points": 0, "rejections": {}} for alias in aliases},
                  "scope": {"tenant_id": ctx.tenant_id, "project_ref": ctx.project_ref, "project_id": ctx.project_id}}
    out: dict[str, list[EcsReading]] = {}
    if source == "NONE":
        for entry in diagnostic["metrics"].values():
            entry.update(status="no_source", reason=unavailable_reason(ctx.tenant))
        return _finish(ctx, out, source, diagnostic, ids)
    if not ids or not aliases:
        return _finish(ctx, out, source, diagnostic, ids)
    for attr in ("tenant_id", "project_ref", "project_id"):
        expected, actual = getattr(ctx, attr, None), getattr(ctx.client, attr, None)
        if expected is not None and actual is not None and expected != actual:
            for alias, entry in diagnostic["metrics"].items():
                _error(ctx, entry, source, alias, "scope_mismatch")
            return _finish(ctx, out, source, diagnostic, ids)
    with request_budget(attempts=384, seconds=60):
        if source == "PROMETHEUS":
            _prometheus(ctx, ids, ip_index, aliases, config, diagnostic, out)
        elif source == "CES":
            with ces_access(ctx.client, use_ces(ctx.tenant)):
                _ces(ctx, ids, aliases, config, diagnostic, out)
    return _finish(ctx, out, source, diagnostic, ids)
