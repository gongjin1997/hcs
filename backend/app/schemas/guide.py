"""资源指南的公开契约；只有字段说明，不包含租户观测或阈值配置。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GuideField(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    label: str
    unit: str | None
    meaning: str
    source: str
    data_type: Literal["number", "integer", "string", "boolean", "object", "array"] = "string"
    optional: bool = True
    availability: Literal["collected", "derived", "not_collected"] = "collected"
    check_items: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ResourceGuide(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    resource_type: str
    name: str
    service: str
    category: str
    ledger: bool
    summary: str
    collection_tasks: list[str]
    metrics: list[GuideField]
    facts: list[GuideField]
    attributes: list[GuideField]
    ledger_fields: list[GuideField]
    guidance: list[str]
    best_practices: list[str]
    limitations: list[str]
