"""ECS 单机监控与历史接口。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

MetricName = Literal["cpu_util", "mem_util", "disk_util"]
TimestampSemantics = Literal["source_sample", "evaluation", "unknown"]


class EcsLatest(BaseModel):
    value: float | None = None
    observed_at: datetime | None = None
    collected_at: datetime | None = None
    source: str | None = None
    unit: str = "%"
    timestamp_semantics: TimestampSemantics = "unknown"
    state: Literal["FRESH", "STALE", "NO_DATA", "DISABLED", "NOT_SELECTED", "SOURCE_UNCONFIGURED",
                   "MAPPING_MISSING", "ERROR"] = "NO_DATA"


class EcsDiagnostic(BaseModel):
    status: str = "NO_DATA"
    message: str = "尚无实际采样"
    at: datetime | None = None
    timestamp_semantics: dict[MetricName, TimestampSemantics] = Field(default_factory=dict)


class EcsInstanceOut(BaseModel):
    id: int
    tenant_id: int | None
    tenant_name: str
    project_ref: int | None
    project_name: str | None = None
    resource_id: str
    name: str
    ip: str | None = None
    status_raw: str | None = None
    health: str
    critical: bool
    monitored: bool
    scope_excluded: bool = False       # 被“采集范围”规则排除（与“纳入巡检”人工开关独立）
    included: bool
    metrics_enabled: bool
    inspection_enabled: bool
    metrics_source: str
    metrics_interval_sec: int
    inspection_interval_sec: int
    stale_after_sec: int
    latest: dict[MetricName, EcsLatest] = Field(default_factory=dict)
    diagnostic: EcsDiagnostic = Field(default_factory=EcsDiagnostic)


class EcsInstancesOut(BaseModel):
    items: list[EcsInstanceOut]
    total: int
    page: int
    page_size: int


class EcsHistoryPoint(BaseModel):
    at: datetime
    average: float | None = None
    maximum: float | None = None
    minimum: float | None = None
    sample_count: int = 0


class EcsHistorySeries(BaseModel):
    metric: MetricName
    label: str
    source: str
    unit: str = "%"
    timestamp_semantics: TimestampSemantics = "unknown"
    points: list[EcsHistoryPoint]


class EcsHistoryOut(BaseModel):
    instance: EcsInstanceOut
    start_at: datetime
    end_at: datetime
    interval_sec: int
    retention_days: int = 90
    generated_at: datetime
    timezone: str = "UTC"
    warnings: list[str] = Field(default_factory=list)
    series: list[EcsHistorySeries]
