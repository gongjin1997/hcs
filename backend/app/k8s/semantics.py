"""Native object semantics shared by collection and event queries (no cloud discovery)."""
import re
from datetime import datetime, timedelta

from app.ledger.fetchers import parse_time


def workload_identity(pod: dict) -> tuple[str, str]:
    """Only an explicit controlling owner is evidence; names/hashes do not prove a parent."""
    meta = pod.get("metadata") or {}
    for owner in meta.get("ownerReferences") or []:
        if owner.get("controller") is True and owner.get("kind") and owner.get("name"):
            return owner["kind"], owner["name"]
    return "Pod", meta.get("name", "")


def validate_metrics_path(path: str) -> str:
    """GET is not sufficient to make an arbitrary application action read-only."""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") \
            or "?" in path or "#" in path or "\\" in path or "%" in path \
            or any(part in ("", ".", "..") for part in path[1:].split("/")) \
            or any(ord(char) <= 32 or ord(char) == 127 for char in path) \
            or path.rsplit("/", 1)[-1] != "metrics":
        raise ValueError("仅允许以 /metrics 结尾、不含查询参数或路径跳转的指标查询路径")
    return path


def label_pairs(value: str) -> dict[str, str]:
    """The configured filter is an explicit list of label=value pairs, not a selector."""
    if not value.strip():
        return {}
    pairs: dict[str, str] = {}
    for part in value.split(","):
        key, separator, label = part.strip().partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", key) or key in pairs:
            raise ValueError("标签过滤须为不重复的 label=value，多个条件使用逗号分隔")
        pairs[key] = label.strip()
    return pairs


def occurrence_interval(first: datetime, last: datetime, count: int) -> dict:
    return {"first": first.isoformat(), "last": last.isoformat(), "count": count}


def event_occurrences(event: dict, now: datetime, *, tolerance: timedelta) -> tuple[datetime, datetime, int]:
    """Normalize core/v1 Event legacy fields and the newer eventTime/series representation.

    A modern singleton has no series; its unused legacy count may be zero/null.
    A present series must supply its own count and lastObservedTime. Invalid
    evidence is rejected with a fixed diagnostic code, never with payload text.
    """
    event_time = parse_time(event.get("eventTime"))
    first = event_time or parse_time(event.get("firstTimestamp")) \
        or parse_time((event.get("metadata") or {}).get("creationTimestamp"))
    series = event.get("series")
    if series is not None:
        if not isinstance(series, dict):
            raise ValueError("event_series_invalid")
        count = series.get("count")
        last = parse_time(series.get("lastObservedTime"))
    else:
        count = event.get("count", 1)
        last = parse_time(event.get("lastTimestamp")) or event_time or first
        if event_time is not None and (count is None or type(count) is int and count == 0):
            count, last = 1, event_time
    if first is None or last is None:
        raise ValueError("event_time_missing")
    if type(count) is not int or count < 1:
        raise ValueError("event_count_invalid")
    if first > last:
        raise ValueError("event_time_reversed")
    if last > now + tolerance:
        raise ValueError("event_time_future")
    return first, last, count


def window_count(event, since: datetime, *, restarts: bool = False, until: datetime | None = None) -> int | None:
    """Cumulative Event.count cannot be placed at lastTimestamp as a batch.

    Intervals contain the initial reported time range and subsequently observed
    count deltas. A boundary crossing is unknown, rather than guessed as zero.
    """
    detail = event.detail or {}
    if restarts and detail.get("restart_counted") is False:
        return 0
    intervals = detail.get("count_intervals")
    if not intervals:
        intervals = [occurrence_interval(event.first_time, event.last_time, event.count)]
    total = 0
    for interval in intervals:
        first, last = parse_time(interval.get("first")), parse_time(interval.get("last"))
        count = interval.get("count")
        if first is None or last is None or first > last or type(count) is not int or count < 0:
            return None
        if last < since:
            continue
        if until is not None and first >= until:
            continue
        if first < since:
            return None
        if until is not None and last >= until:
            return None
        total += count
    return total
