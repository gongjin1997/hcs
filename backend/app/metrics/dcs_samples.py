"""DCS 实际采样点与数值校验；不包含云监控请求。"""
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Literal


@dataclass(frozen=True)
class RawMemoryPoint:
    at: datetime
    memory_usage: float | None = None
    memory_max: float | None = None
    used_memory_mb: float | None = None
    max_memory_mb: float | None = None


def finite_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) and number >= 0 else None


# §4.2.6 表4-81；累计量只取窗口末值，不伪装为 CPU 使用率或 QPS。
DCS_METRICS: dict[str, tuple[str, str, Literal["mean", "last"]]] = {
    "memory_usage": ("内存使用率", "%", "mean"),
    "used_memory_mb": ("已用内存", "MB", "mean"),
    "max_memory_mb": ("内存上限", "MB", "last"),
    "used_cpu_seconds": ("CPU累计时间", "s", "last"),
    "input_kbps": ("网络入流量", "kbit/s", "mean"),
    "output_kbps": ("网络出流量", "kbit/s", "mean"),
    "keys": ("数据条数", "count", "last"),
    "cmd_get_count": ("GET命令累计次数", "count", "last"),
    "cmd_set_count": ("SET命令累计次数", "count", "last"),
}
COUNT_METRICS = ("keys", "cmd_get_count", "cmd_set_count")


def metric_values(values: dict) -> dict[str, float | int | None]:
    from decimal import Decimal, InvalidOperation

    result: dict[str, float | int | None] = {}
    for key in DCS_METRICS:
        raw = values.get(key)
        if key in COUNT_METRICS:
            try:
                count = Decimal(str(raw)) if raw is not None and not isinstance(raw, bool) else Decimal("NaN")
                result[key] = (int(count) if count.is_finite() and count >= 0 and count == count.to_integral_value()
                               and count <= 9223372036854775807 else None)
            except (InvalidOperation, ValueError, OverflowError):
                result[key] = None
        else:
            result[key] = finite_number(raw)
    if result["max_memory_mb"] == 0:
        result["max_memory_mb"] = None
    return result


def parse_observed_at(value: object) -> datetime | None:
    from datetime import UTC

    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
        return at.astimezone(UTC) if at.tzinfo is not None else None
    except ValueError:
        return None
