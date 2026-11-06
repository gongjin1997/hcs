"""规范化已有原生报告，保留真实节点、单位、采样时间；不生成报告或实时值。"""
from datetime import UTC, datetime
from math import isfinite
from typing import Any, Literal

from app.schemas.gaussdb import GaussMetricPoint, GaussMetricSeries


def report_number(value: object) -> float | None:
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def report_timestamp(value: object) -> datetime | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def normalize_metric_series(checks: list[dict[str, Any]], *, warnings: list[str] | None = None) -> list[GaussMetricSeries]:
    result = []
    invalid_times = extra_values = missing_values = invalid_values = invalid_blocks = invalid_summaries = 0
    for check in checks:
        indicators = check.get("indicators")
        if not isinstance(indicators, list):
            continue
        for indicator in indicators:
            if not isinstance(indicator, dict) or not isinstance(indicator.get("metrics"), list):
                continue
            name = _text(indicator.get("name")) or _text(check.get("name")) or "未命名指标"
            identity = " ".join(str(check.get(k) or "") for k in ("item", "name")) + " " + name
            identity = identity.lower()
            kind: Literal["cpu", "memory", "other"] = "memory" if any(
                k in identity for k in ("memory", "mem_usage", "mem_util", "内存")) else (
                "cpu" if "cpu" in identity else "other")
            for metric in indicator["metrics"]:
                if not isinstance(metric, dict):
                    continue
                points = []
                data = metric.get("data")
                chunks = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
                if data is not None and not isinstance(data, dict | list):
                    invalid_blocks += 1
                for chunk in chunks:
                    if not isinstance(chunk, dict) or not isinstance(chunk.get("time"), list):
                        invalid_blocks += 1
                        continue
                    values = chunk.get("metrics")
                    values = values if isinstance(values, list) else []
                    extra_values += max(0, len(values) - len(chunk["time"]))
                    for index, raw_at in enumerate(chunk["time"]):
                        observed_at = report_timestamp(raw_at)
                        if observed_at is None:
                            invalid_times += 1
                            continue
                        if index >= len(values):
                            missing_values += 1
                            value = None
                        else:
                            value = report_number(values[index])
                            if values[index] is not None and value is None:
                                invalid_values += 1
                        points.append(GaussMetricPoint(observed_at=observed_at, value=value))
                points.sort(key=lambda point: point.observed_at)
                summaries = {key: report_number(metric.get(key)) for key in ("min", "max", "average", "p95")}
                invalid_summaries += sum(metric.get(key) is not None and value is None for key, value in summaries.items())
                result.append(GaussMetricSeries(metric=kind, check_name=_text(check.get("name")),
                    check_item=_text(check.get("item")), check_status=_text(check.get("status")), name=name,
                    unit=_text(metric.get("unit")) or _text(indicator.get("unit")),
                    node_name=_text(indicator.get("node")) or _text(metric.get("name")),
                    node_ip=_text(metric.get("ip")), **summaries, points=points))
    if warnings is not None:
        warnings.extend(message for count, message in (
            (invalid_times, f"丢弃无效采样时间 {invalid_times} 个，原始 items 保留"),
            (extra_values, f"丢弃没有配对采样时间的数值 {extra_values} 个，原始 items 保留"),
            (missing_values, f"采样时间缺少配对数值 {missing_values} 个，保留 null 断点"),
            (invalid_values, f"异常采样值 {invalid_values} 个，保留 null 断点"),
            (invalid_blocks, f"丢弃无效指标数据块 {invalid_blocks} 个，原始 items 保留"),
            (invalid_summaries, f"异常指标汇总值 {invalid_summaries} 个，保留空值"),
        ) if count)
    return result
