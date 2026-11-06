"""DCS 内存趋势：查询结果与 XLSX 使用同一时间范围及聚合口径。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

IntervalMinutes = Literal[5, 10, 15, 30, 60]


class DcsInstanceOut(BaseModel):
    id: int
    resource_id: str
    name: str
    tenant_id: int | None = None
    tenant_name: str
    project_ref: int | None = None
    project_name: str | None = None
    region: str | None = None
    status_raw: str | None = None
    memory_usage: float | None = None
    used_memory_mb: float | None = None
    max_memory_mb: float | None = None
    snapshot_at: datetime | None = None
    used_cpu_seconds: float | None = None
    cpu_usage: float | None = None          # 由两次累计 CPU 秒折算的每主节点占用（%）；折算不了时为空
    scope_excluded: bool = False            # 被“采集范围”规则排除：不再采集，页面数据可能已过期
    input_kbps: float | None = None
    output_kbps: float | None = None
    keys: int | None = None
    cmd_get_count: int | None = None
    cmd_set_count: int | None = None
    metrics_source: Literal["DCS", "NONE"] = "NONE"
    api_version: Literal["v2"] | None = None
    source_endpoint: str | None = None
    metrics_observed_at: datetime | None = None
    time_basis: Literal["RESPONSE_RECEIVED"] | None = None
    freshness: Literal["FRESH", "STALE", "NO_DATA", "LEGACY"] = "NO_DATA"
    sample_age_sec: float | None = None
    stale_after_sec: int = 900
    warning: str | None = None


class DcsInstancesOut(BaseModel):
    items: list[DcsInstanceOut]
    total: int
    page: int
    page_size: int


class DcsMemoryPoint(BaseModel):
    at: datetime
    memory_usage: float | None = None
    memory_max: float | None = None
    used_memory_mb: float | None = None
    max_memory_mb: float | None = None
    sample_count: int = 0


class DcsMemorySummary(BaseModel):
    average_usage: float | None = None
    peak_usage: float | None = None
    latest_usage: float | None = None
    latest_at: datetime | None = None


class DcsMemorySeries(BaseModel):
    instance: DcsInstanceOut
    source: Literal["AOM", "CES", "LOCAL", "NONE"]
    status: Literal["AVAILABLE", "PARTIAL", "NO_DATA"]
    warning: str | None = None
    valid_points: int
    total_points: int
    summary: DcsMemorySummary
    points: list[DcsMemoryPoint]


class DcsHistoryOut(BaseModel):
    start_at: datetime
    end_at: datetime
    interval_minutes: IntervalMinutes
    timezone: str
    demo: bool = False
    generated_at: datetime
    series: list[DcsMemorySeries] = Field(default_factory=list)


class DcsMetricDefinition(BaseModel):
    key: str
    label: str
    unit: str
    aggregation: Literal["mean", "last"]


class DcsMetricPoint(DcsMemoryPoint):
    used_cpu_seconds: float | None = None
    input_kbps: float | None = None
    output_kbps: float | None = None
    keys: int | None = None
    cmd_get_count: int | None = None
    cmd_set_count: int | None = None
    last_observed_at: datetime | None = None
    metric_sample_counts: dict[str, int] = Field(default_factory=dict)


class DcsMetricSummary(BaseModel):
    average: float | None = None
    peak: float | int | None = None
    latest: float | int | None = None
    latest_at: datetime | None = None
    sample_count: int = 0


class DcsMetricSeries(BaseModel):
    instance: DcsInstanceOut
    source: Literal["LOCAL", "NONE"]
    api_version: Literal["v2"] = "v2"
    status: Literal["AVAILABLE", "PARTIAL", "NO_DATA"]
    warning: str | None = None
    valid_points: int
    total_points: int
    summary: DcsMemorySummary
    summaries: dict[str, DcsMetricSummary]
    points: list[DcsMetricPoint]


class DcsMetricsHistoryOut(BaseModel):
    start_at: datetime
    end_at: datetime
    interval_minutes: IntervalMinutes
    timezone: str
    demo: bool = False
    generated_at: datetime
    metrics: list[DcsMetricDefinition]
    series: list[DcsMetricSeries] = Field(default_factory=list)
