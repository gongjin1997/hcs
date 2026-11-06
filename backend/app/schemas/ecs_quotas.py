"""ECS 项目实际配额（不是默认配额）；缺失值与无限制均不序列化为 0。"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field


class EcsQuotaItem(BaseModel):
    key: str
    label: str
    unit: Literal["count", "vCPU", "MB", "byte"]
    limit: int | None = Field(None, ge=0)
    used: int | None = Field(None, ge=0)
    limit_status: Literal["known", "unlimited", "unknown"] = "unknown"
    usage_status: Literal["known", "unknown", "not_reported"] = "unknown"
    remaining: int | None = Field(None, description="已知限额减已知用量；负数表示超额，无限制/未知时为 null")
    limit_field: str
    usage_field: str | None = None
    note: str | None = None


class EcsQuotaOut(BaseModel):
    tenant_id: int = Field(description="应用 hcs_account 外键，不是 HCS API 路径中的 tenant_id")
    tenant_name: str
    project_ref: int = Field(description="应用 hcs_project 外键")
    project_id: str = Field(description="实际 HCS 项目 ID；PDF §5.10 将其命名为 tenant_id")
    project_name: str
    region: str
    enabled: bool = Field(description="租户、项目与该租户生效的 ECS 主采集开关均启用")
    source: Literal["ecs.native_limits"] = "ecs.native_limits"
    status: Literal["not_collected", "available", "partial", "refresh_failed"]
    fetched_at: AwareDatetime | None = Field(None, description="最近一次成功取得原生 limits 响应的 UTC 时间")
    source_observed_at: None = Field(None, description="原生 limits 没有观测时间字段，不用本地时间冒充")
    last_attempt_at: AwareDatetime | None = None
    last_error: Literal["upstream_error", "invalid_response", "configuration_error"] | None = None
    is_stale: bool = Field(False, description="最近刷新失败且保留旧值；不假定任何定时采集频率")
    quotas: list[EcsQuotaItem]


class EcsQuotasOut(BaseModel):
    items: list[EcsQuotaOut]
    total: int
    page: int
    page_size: int
