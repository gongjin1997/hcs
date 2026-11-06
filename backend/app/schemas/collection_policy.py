"""Tenant collection policy contract; no setting enables the master collect switch."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.metrics.prometheus import DEFAULT_QUERIES

Metric = Literal["cpu_util", "mem_util", "disk_util", "boot_time"]
METRICS = {"cpu_util", "mem_util", "disk_util", "boot_time"}


def default_metric_keys() -> list[Metric]:
    return ["cpu_util", "mem_util", "disk_util", "boot_time"]


class StagePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(True, strict=True)
    interval_sec: int = Field(300, strict=True, ge=30, le=30 * 86400)


class SelectionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["all", "critical", "selected"] = "all"
    resource_ids: list[int] = Field(default_factory=list, max_length=20000)

    @field_validator("resource_ids", mode="before")
    @classmethod
    def local_ids(cls, value):
        if not isinstance(value, list) or any(type(v) is not int or v <= 0 for v in value):
            raise ValueError("资源 ID 必须是台账 Resource.id 的正整数")
        if len(value) != len(set(value)):
            raise ValueError("资源 ID 不可重复")
        return value


class PrometheusMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queries: dict[str, str] = Field(default_factory=dict, max_length=4)
    instance_label: str = Field("instance", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    units: dict[str, str] = Field(default_factory=dict, max_length=4)
    timestamp_queries: dict[str, str] = Field(default_factory=dict, max_length=4)
    label_filters: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("queries", "timestamp_queries")
    @classmethod
    def safe_queries(cls, value: dict[str, str]) -> dict[str, str]:
        # Queries are sent only to the tenant's configured read-only /api/v1/query.
        # Reject controls, URL injection and unbounded remote query payloads.
        if not set(value) <= METRICS or any(
            not query.strip() or len(query) > 4096 or re.search(r"[\x00-\x1f\x7f]", query)
            or re.search(r"https?://", query, re.I) for query in value.values()
        ):
            raise ValueError("Prometheus 映射仅允许受支持指标的安全只读查询（最长 4096 字符）")
        return {key: query.strip() for key, query in value.items()}

    @field_validator("units")
    @classmethod
    def safe_units(cls, value: dict[str, str]) -> dict[str, str]:
        for key, unit in value.items():
            allowed = {"s", "sec", "second", "seconds"} if key == "boot_time" else {"%", "percent", "percentage", "百分比"}
            if key not in METRICS or unit.strip().lower() not in allowed:
                raise ValueError("使用率单位须为百分比，开机时间单位须为秒；不自动换算未知单位")
        return {key: unit.strip() for key, unit in value.items()}

    @field_validator("label_filters")
    @classmethod
    def safe_labels(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key)
               or not label.strip() or len(label) > 1024 or re.search(r"[\x00-\x1f\x7f]", label)
               for key, label in value.items()):
            raise ValueError("标签过滤须为合法标签名到非空字符串的精确匹配，值最长 1024 字符")
        return value  # 标签值不 trim，不猜测本地租户/项目到云标签的映射。

class CollectionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inventory: StagePolicy = Field(default_factory=lambda: StagePolicy(enabled=True, interval_sec=86400))
    metrics: StagePolicy = Field(default_factory=lambda: StagePolicy(enabled=True, interval_sec=300))
    inspection: StagePolicy = Field(default_factory=lambda: StagePolicy(enabled=True, interval_sec=120))
    quota: StagePolicy = Field(default_factory=lambda: StagePolicy(enabled=False, interval_sec=3600))
    metric_keys: list[Metric] = Field(default_factory=default_metric_keys)
    selection: SelectionPolicy = Field(default_factory=SelectionPolicy)
    stale_after_sec: int = Field(900, strict=True, ge=60, le=30 * 86400)
    aom: None = None  # 仅接受 null 清理旧配置，不允许新增或恢复 AOM 映射。
    prometheus: PrometheusMapping | None = None

    @field_validator("metric_keys")
    @classmethod
    def unique_metrics(cls, value: list[Metric]) -> list[Metric]:
        if len(value) != len(set(value)):
            raise ValueError("指标不可重复")
        return value


class CollectionPolicyUpdate(CollectionPolicy):
    """写入严格要求单位；读取历史缺单位配置不补单位，由 provider 报未知。"""

    @model_validator(mode="after")
    def explicit_custom_units(self):
        mapping = self.prometheus
        if mapping is not None and any(query != DEFAULT_QUERIES[key] and key not in mapping.units
                for key, query in mapping.queries.items()):
            raise ValueError("自定义 PromQL 必须显式声明单位（使用率为 %，开机时间为 s）")
        return self


class PhaseStatus(BaseModel):
    configured_enabled: bool
    master_enabled: bool
    task_code: str
    task_enabled: bool
    supported: bool
    effective_enabled: bool
    reason: Literal["unsupported", "master_disabled", "stage_disabled", "task_disabled", "invalid_schedule", "enabled"]
    schedule_source: Literal["policy", "task", "default"]
    interval_sec: int
    cron: str | None


class CollectionPolicyOut(BaseModel):
    tenant_id: int
    resource_type: Literal["ecs_server"]
    master_enabled: bool
    policy: CollectionPolicy
    phase_status: dict[str, PhaseStatus]


class CollectionPolicySaveOut(CollectionPolicyOut):
    closed_alarms: int
