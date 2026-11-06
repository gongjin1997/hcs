"""容器事件 / Pod 状态 / 背压探针。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.k8s.semantics import label_pairs, validate_metrics_path

from .common import ORMModel


class PodEventOut(ORMModel):
    id: int
    tenant_id: int | None = None
    project_ref: int | None = None
    tenant_name: str | None = None
    cluster_id: str
    cluster_name: str
    namespace: str
    workload_kind: str = ""
    workload: str = ""
    pod: str = ""
    container: str = ""
    node: str | None = None
    event_type: str
    type_name: str = ""
    color: str = ""
    level: str
    reason: str = ""
    message: str = ""
    source: str
    count: int = 1
    first_time: datetime
    last_time: datetime
    end_time: datetime | None = None
    ongoing: bool = False
    peak: float | None = None
    duration_sec: int | None = None
    detail: dict = {}


PodFreshnessStatus = Literal["FRESH", "STALE", "UNKNOWN", "PARTIAL", "DELETED"]


class PodFreshnessOut(BaseModel):
    status: PodFreshnessStatus = "UNKNOWN"
    last_observed_at: datetime | None = None
    age_sec: int | None = None
    stale_after_sec: int | None = None


class PodStateOut(ORMModel):
    key: str
    tenant_id: int | None = None
    project_ref: int | None = None
    tenant_name: str | None = None
    cluster_id: str
    cluster_name: str
    namespace: str
    pod: str
    workload_kind: str = ""
    workload: str = ""
    node: str | None = None
    pod_ip: str | None = None
    phase: str = ""
    ready: bool = False
    status_reason: str | None = None
    restarts: int = 0
    started_at: datetime | None = None
    labels: dict = {}
    containers: list = []
    deleted: bool = False
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    freshness: PodFreshnessOut = Field(default_factory=PodFreshnessOut)


class ProbeIn(BaseModel):
    tenant_id: int | None = None          # 为空时取当前租户（用户只有一个租户 / 头部已切换租户）
    name: str = Field(min_length=1, max_length=64)
    cluster_id: str = Field("*", min_length=1, max_length=64)
    namespace: str = Field(min_length=1, max_length=128)
    selector: str = Field("", max_length=255)
    port: int = Field(9249, ge=1, le=65535)
    path: str = Field("/metrics", max_length=128)
    metric: str = Field(min_length=1, max_length=160)
    label_filter: str = Field("", max_length=255)
    agg: str = Field("max", pattern="^(max|avg|sum)$")
    op: str = Field(">=", pattern="^(>=|>|<=|<)$")
    threshold: float = Field(allow_inf_nan=False)
    unit: str = Field("", max_length=16)
    for_minutes: int = Field(3, ge=1, le=120)
    recover_minutes: int = Field(5, ge=1, le=240)
    enabled: bool = True

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        v = (v or "/metrics").strip()
        return validate_metrics_path(v if v.startswith("/") else f"/{v}")

    @field_validator("metric")
    @classmethod
    def _metric(cls, v: str) -> str:
        import re

        v = v.strip()
        if not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", v):
            raise ValueError("指标名格式不正确（Prometheus 文本格式的指标名，"
                             "例如 flink_taskmanager_job_task_backPressuredTimeMsPerSecond）")
        return v

    @field_validator("label_filter")
    @classmethod
    def _filter(cls, v: str) -> str:
        label_pairs(v)
        return v.strip()


class ProbeOut(ProbeIn, ORMModel):
    id: int
    tenant_id: int | None = None
    tenant_name: str | None = None
    last_result: dict = {}
    updated_by: str | None = None
    updated_at: datetime | None = None

    # Historical unsupported configuration must remain readable so an operator can fix it.
    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        return v

    @field_validator("label_filter")
    @classmethod
    def _filter(cls, v: str) -> str:
        return v
