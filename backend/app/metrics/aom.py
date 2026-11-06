"""旧 AOM 数据结构兼容；当前版本禁止任何 AOM 查询、探测或缓存读取。

保留函数签名用于旧调用方快速得到明确的停用结果，不提供可重新启用的开关。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.hcs import HcsClient, HcsError

if TYPE_CHECKING:
    from app.collectors.base import RunContext


class AomMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: str = Field(min_length=1, max_length=255)
    dimension: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,254}$")
    metrics: dict[str, str] = Field(min_length=1, max_length=16)
    units: dict[str, str] = Field(default_factory=dict, max_length=16)
    period: Literal[60, 300, 900, 3600] = 300
    window_sec: int = Field(900, ge=60, le=86400)
    prometheus_instance_id: str | None = Field(None, pattern=r"^[A-Za-z0-9_.:-]{1,128}$")

    @field_validator("namespace")
    @classmethod
    def _namespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("AOM namespace 不能为空")
        return value.strip()

    @field_validator("metrics", "units")
    @classmethod
    def _names(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key)
               or not name.strip() or len(name) > 255 for key, name in value.items()):
            raise ValueError("AOM 指标映射须为事实字段到真实指标名/单位的非空字符串映射")
        return {key: name.strip() for key, name in value.items()}


@dataclass(frozen=True)
class AomPoint:
    at: datetime
    unit: str | None
    statistics: dict[str, float]


@dataclass
class AomSeries:
    metric: dict[str, Any]
    points: list[AomPoint]
    partial: bool = False


def _disabled() -> NoReturn:
    raise HcsError(0, "LOCAL.AOM_DISABLED", "当前版本已停用 AOM，未查询或读取 AOM 指标")


def aom_list_metrics(client: HcsClient, namespace: str, metric_name: str | None = None,
                     dimensions: list[dict] | None = None, *, limit: int = 1000, all_pages: bool = True,
                     prometheus_instance_id: str | None = None, descriptors: list[dict] | None = None) -> list[dict]:
    _disabled()


def aom_probe(client: HcsClient) -> None:
    _disabled()


def discover(client: HcsClient, descriptors: list[dict], *, prometheus_instance_id: str | None = None) -> list[dict]:
    _disabled()


def aom_query(client: HcsClient, metrics: list[dict], start: datetime, end: datetime, period: int = 300,
              statistics: tuple[str, ...] = ("average", "maximum"),
              prometheus_instance_id: str | None = None) -> list[AomSeries]:
    _disabled()


def resolve_series(client: HcsClient, resource_ids: list[str], mapping: AomMapping) -> dict[tuple[str, str], dict]:
    _disabled()


def aom_latest(ctx: RunContext, resource_ids: list[str]) -> dict[str, dict[str, float]]:
    ctx.stats.update(aom_disabled=True, aom_mapping_configured=False)
    return {}
