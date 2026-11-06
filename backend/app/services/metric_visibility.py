"""页面、台账和报表共用当前指标口径；旧 AOM 数据不作为当前值。"""
from datetime import datetime
from typing import TYPE_CHECKING

from app.metrics.dcs_samples import DCS_METRICS, finite_number, parse_observed_at
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.models import Resource
    from app.schemas.ecs import EcsInstanceOut


def current_metrics(resource: "Resource", ecs: "EcsInstanceOut | None" = None, *,
                    now: datetime | None = None) -> dict:
    now = now or utcnow()
    metrics = dict(resource.metrics or {})
    attrs = resource.attrs or {}
    meta = attrs.get("ecs_monitoring")
    if (resource.resource_type != "ecs_server" and (attrs.get("metrics_source") == "AOM"
            or isinstance(meta, dict) and meta.get("source") == "AOM")):
        return {key: None for key in metrics}
    if resource.resource_type == "dcs_instance":
        from app.services.dcs_monitoring import native_current

        native = native_current(resource, now)
        metrics.update({key: native[key] for key in DCS_METRICS})
        metrics.update(cpu_usage=None, connected_clients=None)
    elif resource.resource_type == "gaussdb_instance":
        meta = attrs.get("gaussdb_monitoring")
        meta = meta if isinstance(meta, dict) else {}
        observed = parse_observed_at(meta.get("observed_at"))
        max_age = meta.get("stale_after_sec", 900)
        if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age < 60:
            max_age = 900
        fresh = (attrs.get("metrics_source") == "GAUSSDB" and meta.get("source") == "GAUSSDB"
                 and meta.get("api_version") == "v3" and meta.get("endpoint") == "volume-usage"
                 and meta.get("time_basis") == "RESPONSE_RECEIVED" and observed is not None
                 and -30 <= (now - observed).total_seconds() <= max_age)
        for key in ("disk_usage", "disk_used_gb", "disk_total_gb"):
            metrics[key] = finite_number(metrics.get(key)) if fresh else None
        metrics.update(cpu_usage=None, mem_usage=None)
    elif resource.resource_type != "ecs_server":
        from app.services.inspection_evidence import runtime_metric_keys
        from app.services.resource_guides import get_resource_guide

        managed = runtime_metric_keys(resource.resource_type)
        if managed:
            meta = attrs.get("metrics_monitoring")
            meta = meta if isinstance(meta, dict) else {}
            observed = parse_observed_at(meta.get("observed_at"))
            max_age = meta.get("stale_after_sec", 900)
            assessment = attrs.get("inspection_monitoring")
            missing = (isinstance(assessment, dict) and assessment.get("checked_rules") == 0
                       and isinstance(assessment.get("unavailable_rules"), int)
                       and assessment["unavailable_rules"] > 0)
            fresh = (not missing and meta.get("time_basis") == "LOCAL_COLLECTION" and observed is not None
                     and type(max_age) is int and max_age >= 60
                     and -30 <= (now - observed).total_seconds() <= max_age)
            for metric in managed:
                metrics[metric] = finite_number(metrics.get(metric)) if fresh else None
            guide = get_resource_guide(resource.resource_type)
            if guide:
                metrics.update({field.key: None for field in guide.metrics if field.availability == "not_collected"})
    if resource.resource_type == "ecs_server":
        for ecs_key in ("cpu_util", "mem_util", "disk_util"):
            point = ecs.latest.get(ecs_key) if ecs else None
            metrics[ecs_key] = point.value if point is not None and point.state == "FRESH" else None
        disk = ecs.latest.get("disk_util") if ecs else None
        if disk is None or disk.state != "FRESH":
            metrics["disk_mount"] = None
    return metrics
