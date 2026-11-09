"""巡检有效性独立于台账同步时间；读接口不会修改持久化资源或告警。"""
from datetime import datetime

from app.metrics.dcs_samples import parse_observed_at
from app.models import Resource
from app.models.base import utcnow

ALARM_HEALTH = {"CRITICAL", "MAJOR", "WARNING", "INFO"}


def _result_state(raw: object, now: datetime, *, allow_ruleless: bool = False) -> str:
    if not isinstance(raw, dict):
        return "NO_DATA"
    at = parse_observed_at(raw.get("evaluated_at"))
    checked, missing = raw.get("checked_rules"), raw.get("unavailable_rules")
    threshold = raw.get("stale_after_sec", 900)
    valid_count = type(checked) is int and (checked >= 1 or checked == 0 and allow_ruleless
        and raw.get("no_applicable_rules") is True and raw.get("observation_available") is True and missing == 0)
    if (at is None or not valid_count or type(missing) is not int or missing < 0
            or type(threshold) is not int or threshold < 60):
        return "NO_DATA"
    age = (now - at).total_seconds()
    if age < -30:
        return "NO_DATA"
    if age > threshold:
        return "STALE"
    return "PARTIAL" if missing else "FRESH"


def inspection_state(resource: Resource, now: datetime | None = None) -> str:
    if not resource.monitored or resource.scope_excluded or resource.lifecycle != "ACTIVE":
        return "DISABLED"
    raw = (resource.attrs or {}).get("inspection_monitoring")
    now = now or utcnow()
    if resource.resource_type == "gaussdb_instance" and isinstance(raw, dict) and "tasks" in raw:
        tasks, required = raw.get("tasks"), raw.get("required_tasks")
        if (not isinstance(tasks, dict) or not isinstance(required, list)
                or any(not isinstance(code, str) or not code for code in required)):
            return "NO_DATA"
        if not required:
            return "DISABLED"
        states = [_result_state(result, now, allow_ruleless=True) if isinstance(result := tasks.get(code), dict)
                  and result.get("task_code") == code else "NO_DATA" for code in required]
        if all(state == "FRESH" for state in states):
            return "FRESH"
        if "STALE" in states:
            return "STALE"
        return "NO_DATA" if all(state == "NO_DATA" for state in states) else "PARTIAL"
    return _result_state(raw, now)


def current_health(resource: Resource, now: datetime | None = None) -> str:
    # 数据失联不能解除已有异常；恢复仍由真实观测和告警状态机决定。
    if resource.health in ALARM_HEALTH:
        return resource.health
    return "OK" if inspection_state(resource, now) == "FRESH" else "UNKNOWN"
