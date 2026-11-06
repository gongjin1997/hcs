"""GaussDB 原生诊断面板；使用量、配置量和巡检报告分别标注。"""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

NativeStatus = Literal["AVAILABLE", "NO_DATA", "PARTIAL", "UNAVAILABLE", "ERROR", "DISABLED", "UNSUPPORTED"]


StatusGroup = Literal["abnormal", "transitional", "stopped", "normal"]
Freshness = Literal["FRESH", "STALE", "NO_DATA"]


class GaussComponent(BaseModel):
    node_id: str
    node_name: str | None = None
    availability_zone_id: str | None = None
    component_id: str
    component_type: str
    role: str | None = None
    status: str | None = None
    distributed_id: str | None = Field(default=None, description="DN 分组（分片）ID；其他组件为空")


class GaussComponentSummary(BaseModel):
    """最近一次巡检采集的组件汇总（本地，不请求云）。"""
    state: Literal["AVAILABLE", "NO_DATA", "STALE", "ERROR"]
    observed_at: datetime | None = Field(default=None, description="本地收到组件响应的时间")
    freshness: Freshness = "NO_DATA"
    total: int | None = None
    down: int | None = None
    unknown: int | None = None
    by_type: dict[str, int] = Field(default_factory=dict)
    warning: str | None = None


class GaussNode(BaseModel):
    name: str | None = None
    role: str | None = None
    status: str | None = None


class GaussInstance(BaseModel):
    id: int
    resource_id: str
    name: str
    tenant_id: int | None = None
    tenant_name: str
    project_ref: int | None = None
    project_name: str | None = None
    region: str | None = None
    status_raw: str | None = None
    health: str
    scope_excluded: bool = False            # 被“采集范围”规则排除：不再采集
    vcpus: int | None = None
    memory_size: float | None = None
    memory_unit: str | None = None
    storage_size: float | None = None
    storage_unit: str | None = None
    snapshot_at: datetime | None = None
    # 以下均来自本地巡检记录，列表与详情一致；不触发云请求
    status_group: StatusGroup = "normal"
    status_since: datetime | None = None
    deploy_type: str | None = None
    version: str | None = None
    disk_usage: float | None = None
    disk_used_gb: float | None = None
    disk_total_gb: float | None = None
    disk_state: Literal["AVAILABLE", "PARTIAL", "ERROR", "UNAVAILABLE"] | None = None
    disk_observed_at: datetime | None = Field(default=None, description="本地收到 volume-usage 响应的时间")
    disk_freshness: Freshness = "NO_DATA"
    node_count: int | None = None
    components: GaussComponentSummary | None = None
    active_alarms: int = 0
    # 只在单实例详情返回
    nodes: list[GaussNode] | None = None
    component_items: list[GaussComponent] | None = None


class GaussSummaryOut(BaseModel):
    """当前可见范围的 GaussDB 概况（本地数据）。"""
    total: int = 0
    abnormal: int = 0
    transitional: int = 0
    stopped: int = 0
    normal: int = 0
    scope_excluded: int = 0
    disk_high: int = Field(0, description="最近一次采集磁盘使用率 ≥ 85% 的实例数（只计未过期的采集）")
    components_down: int = Field(0, description="最近一次采集有组件宕机的实例数")
    active_alarms: int = 0
    last_collected_at: datetime | None = None


class GaussInstancesOut(BaseModel):
    items: list[GaussInstance]
    total: int
    page: int
    page_size: int


class GaussProjectOption(BaseModel):
    project_ref: int
    project_name: str
    tenant_id: int
    tenant_name: str
    region: str | None = None


class GaussDisk(BaseModel):
    status: NativeStatus
    used_gb: float | None = None
    total_gb: float | None = None
    usage_percent: float | None = None
    observed_at: datetime | None = Field(default=None, description="兼容字段：本地响应接收时间，不是源端采样时间")
    collected_at: datetime | None = Field(default=None, description="本地接收到 volume-usage 响应的时间；源端未提供采样时间")
    time_basis: Literal["RESPONSE_RECEIVED"] | None = None
    warning: str | None = None


class GaussOverviewOut(BaseModel):
    instance: GaussInstance
    source: Literal["GAUSSDB"] = "GAUSSDB"
    queried_at: datetime
    disk: GaussDisk
    components: list[GaussComponent] = Field(default_factory=list)
    components_status: NativeStatus
    components_warning: str | None = None
    cpu_usage: None = None
    memory_usage: None = None
    performance_note: str = "CPU、内存见原生巡检报告；报告指标按其检查时间展示，不是当前使用率。"


class GaussDiskHistoryPoint(BaseModel):
    timestamp: datetime = Field(description="查询时间桶起点，不是源端采样时间或本地获取时间")
    observed_at: datetime | None = Field(default=None, description="兼容字段：本桶最后一次成功响应的本地接收时间")
    collected_at: datetime | None = Field(default=None, description="本桶最后一次成功响应的本地接收时间；失败或缺失为空")
    used_gb: float | None = None
    total_gb: float | None = None
    usage_percent: float | None = None


class GaussDiskHistoryOut(BaseModel):
    instance: GaussInstance
    source: Literal["GAUSSDB"] = "GAUSSDB"
    sampling_source: Literal["LOCAL_COLLECTOR"] = "LOCAL_COLLECTOR"
    time_basis: Literal["RESPONSE_RECEIVED"] = "RESPONSE_RECEIVED"
    status: NativeStatus
    queried_at: datetime
    start_at: datetime
    end_at: datetime
    period_sec: int
    points: list[GaussDiskHistoryPoint] = Field(default_factory=list)
    warning: str | None = None


class GaussMetricPoint(BaseModel):
    observed_at: datetime = Field(description="原生巡检报告 data.time 的采样时间，不是本地获取时间或报告执行时间")
    value: float | None = None


class GaussMetricSeries(BaseModel):
    metric: Literal["cpu", "memory", "other"]
    check_name: str | None = None
    check_item: str | None = None
    check_status: str | None = None
    name: str
    unit: str | None = None
    node_name: str | None = None
    node_ip: str | None = None
    min: float | None = None
    max: float | None = None
    average: float | None = None
    p95: float | None = None
    points: list[GaussMetricPoint] = Field(default_factory=list)


class GaussNativeRows(BaseModel):
    source: Literal["GAUSSDB"] = "GAUSSDB"
    status: NativeStatus
    queried_at: datetime
    items: list[dict[str, Any]] = Field(default_factory=list)
    total: int | None = None
    truncated: bool = False
    warning: str | None = None
    units: dict[str, str] = Field(default_factory=dict)
    start_at: datetime | None = None
    end_at: datetime | None = None
    record_id: str | None = None
    metric_series: list[GaussMetricSeries] | None = None
