"""巡检有效性独立于台账同步时间；读接口不会修改持久化资源或告警。"""
from datetime import datetime

from app.metrics.dcs_samples import parse_observed_at
from app.models import Resource
from app.models.base import utcnow

ALARM_HEALTH = {"CRITICAL", "MAJOR", "WARNING", "INFO"}


def inspection_state(resource: Resource, now: datetime | None = None) -> str:
    if not resource.monitored or resource.scope_excluded or resource.lifecycle != "ACTIVE":
        return "DISABLED"
    raw = (resource.attrs or {}).get("inspection_monitoring")
    if not isinstance(raw, dict):
        return "NO_DATA"
    at = parse_observed_at(raw.get("evaluated_at"))
    checked, missing = raw.get("checked_rules"), raw.get("unavailable_rules")
    threshold = raw.get("stale_after_sec", 900)
    if (at is None or type(checked) is not int or checked < 1 or type(missing) is not int or missing < 0
            or type(threshold) is not int or threshold < 60):
        return "NO_DATA"
    age = ((now or utcnow()) - at).total_seconds()
    if age < -30:
        return "NO_DATA"
    if age > threshold:
        return "STALE"
    return "PARTIAL" if missing else "FRESH"


def current_health(resource: Resource, now: datetime | None = None) -> str:
    # 数据失联不能解除已有异常；恢复仍由真实观测和告警状态机决定。
    if resource.health in ALARM_HEALTH:
        return resource.health
    return "OK" if inspection_state(resource, now) == "FRESH" else "UNKNOWN"
