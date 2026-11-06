from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.catalog import RESOURCE_TYPES

from .common import ORMModel


class ResourceOut(ORMModel):
    id: int
    tenant_id: int | None = None
    service: str
    resource_type: str
    resource_id: str
    name: str
    status_raw: str | None = None
    health: str
    region: str | None = None
    az: str | None = None
    ip: str | None = None
    spec: str | None = None
    vcpus: int | None = None
    memory_gb: float | None = None
    storage_gb: float | None = None
    created_time: datetime | None = None
    labels: dict
    attrs: dict
    metrics: dict
    expected_state: dict
    owner_user_id: int | None = None
    owner: str | None = None
    classification: str | None = None
    classification_source: str | None = None
    business: str | None = None
    env_type: str | None = None
    remark: str | None = None
    monitored: bool
    critical: bool
    lifecycle: str
    first_seen: datetime
    last_seen: datetime
    deleted_at: datetime | None = None
    status_since: datetime | None = None
    active_alarms: int = 0


class ResourcePatch(BaseModel):
    """台账管理字段（同步不会覆盖）。"""

    owner_user_id: int | None = None
    owner: str | None = None
    business: str | None = None
    env_type: str | None = None
    remark: str | None = None
    critical: bool | None = None
    monitored: bool | None = None
    expected_state: dict | None = None

    @field_validator("critical", "monitored", mode="before")
    @classmethod
    def _boolean_flags(cls, value):
        if type(value) is not bool:
            raise ValueError("巡检及核心资源开关须为 true 或 false")
        return value

    @field_validator("expected_state")
    @classmethod
    def _expected_running(cls, value):
        if value is not None and "running" in value and type(value["running"]) is not bool:
            raise ValueError("预期运行状态须为 true 或 false")
        return value


class ChangeOut(ORMModel):
    id: int
    tenant_id: int | None = None
    resource_ref: int
    resource_type: str
    resource_id: str
    name: str
    change_type: str
    changes: list
    source: str
    operator: str
    detected_at: datetime


class ClassificationRule(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=64)
    resource_type: str
    field: Literal["os", "name", "business", "spec"]
    pattern: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=64)
    enabled: bool = True

    @field_validator("resource_type")
    @classmethod
    def _resource_type(cls, value: str) -> str:
        if value not in RESOURCE_TYPES:
            raise ValueError("资源类型不在资源目录中")
        return value


class ClassificationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rules: list[ClassificationRule] = Field(max_length=200)
    fallback: str = Field(default="其他", min_length=1, max_length=64)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("分类规则 ID 不能重复")
        return self
