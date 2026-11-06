"""本地监控覆盖契约；能力说明和实际巡检覆盖分开表达。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.guide import GuideField

MonitoringResourceType = Literal["ecs_server", "dcs_instance", "gaussdb_instance"]
CoverageState = Literal[
    "tenant_disabled", "collection_disabled", "no_projects", "projects_disabled", "inspection_disabled",
    "configuration_invalid", "no_resources", "no_eligible_resources", "not_inspected", "stale", "partial", "covered",
]
CoverageBlocker = Literal[
    "tenant_disabled", "collection_disabled", "no_projects", "projects_disabled", "ledger_task_disabled",
    "inspection_task_disabled", "metrics_task_disabled", "ledger_phase_disabled", "inspection_phase_disabled",
    "metrics_phase_disabled", "invalid_policy", "invalid_schedule", "no_eligible_resources",
]


class MonitoringMetric(BaseModel):
    key: str
    label: str
    unit: str | None
    availability: Literal["collected", "derived", "not_collected"]

    @classmethod
    def from_guide(cls, field: GuideField) -> "MonitoringMetric":
        return cls(key=field.key, label=field.label, unit=field.unit, availability=field.availability)


class MonitoringCoverageRow(BaseModel):
    tenant_id: int
    tenant_name: str
    resource_type: MonitoringResourceType
    resource_name: str
    resources: int = Field(ge=0)
    eligible_resources: int = Field(ge=0, description="当前启用租户、项目、巡检及选择范围中的活动资源")
    inspected_resources: int = Field(ge=0, description="当前可巡检且 inspection_state=FRESH 的资源数")
    unknown_resources: int = Field(ge=0, description="无有效完整巡检且无已知异常的资源数；不计为健康")
    abnormal_resources: int = Field(ge=0, description="保留已知异常；可能同时有有效巡检或过期证据")
    partial_resources: int = Field(ge=0, description="当前可巡检且 inspection_state=PARTIAL 的资源数")
    stale_resources: int = Field(ge=0, description="活动资源中 inspection_state=STALE 的资源数")
    last_inspected_at: datetime | None = Field(description="最近真实巡检求值时间，可过期；不是指标时间")
    projects: int = Field(ge=0)
    enabled_projects: int = Field(ge=0)
    collection_enabled: bool
    ledger_enabled: bool
    inspection_enabled: bool
    metrics_enabled: bool = Field(description="指标任务/阶段及租户、项目等启用条件生效；不证明指标源已配置或已有有效样本")
    tenant_enabled: bool
    ledger_task_enabled: bool
    inspection_task_enabled: bool
    metrics: list[MonitoringMetric]
    state: CoverageState = Field(description="本地巡检覆盖状态；covered 只表示有效完整的巡检证据，不承诺指标覆盖或实时性能可用")
    state_label: str
    blockers: list[CoverageBlocker]
    blocker_labels: list[str] = Field(description="与 blockers 严格同序的可读说明，统一使用后端字典")
    source_label: str


class MonitoringCoverageOut(BaseModel):
    evaluated_at: datetime
    rows: list[MonitoringCoverageRow]
