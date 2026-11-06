import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.hcs import HcsClient, HcsError

from .scope import OPEN, ScopeFilter

_SAFE_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


@dataclass
class Observation:
    """采集器输出的标准化观测：某资源（或其子对象）在某巡检项上的事实集合。

    - facts：供规则表达式使用的扁平字段，例如 {"status": "RUNNING", "memory_usage": 92.3}
    - status_raw / attrs / metrics：回写 CMDB（资源中心列表展示用）
    """

    resource_type: str
    resource_id: str
    name: str
    check_item: str
    facts: dict
    sub_object: str = ""
    status_raw: str | None = None
    attrs: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    # 采样元数据不进入可配置规则的业务事实字段。
    metric_observed_at: datetime | None = None
    metric_config_signature: str | None = None


@dataclass
class RunContext:
    client: HcsClient
    account: str                                      # 租户代码（告警指纹的一部分）
    project_id: str
    workspaces: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)   # 局部失败（某个工作空间 / 集群），任务记为 PARTIAL
    tenant_id: int | None = None
    tenant_name: str = ""
    project_ref: int | None = None
    region: str = ""
    db: Any = None                                    # 需要读写本地状态的采集器（容器事件、ECS 状态）使用
    tenant: Any = None                                # HcsAccount：监控指标来源、能力探测结果（CES 是否可用）
    stats: dict = field(default_factory=dict)         # 写入运行记录 stats（例如新增容器事件数、指标来源）
    scopes: Any = None                                # 采集范围（services.collect_scope.Scopes）；None = 不裁剪

    def scope(self, resource_type: str) -> ScopeFilter:
        """本租户该资源类型的采集范围。采集器在调用云接口之前用它丢弃确定在范围外的对象，
        只用已知的维度提问（其余维度不给），拿不准时保留，最终判定在巡检流水线。"""
        return self.scopes.get(self.tenant_id, resource_type) if self.scopes is not None else OPEN


def previous_attrs(ctx: RunContext, resource_type: str, ids: list[str], key: str) -> dict[str, dict]:
    """上一次保存在资源记录里的某个对象型属性（只读本租户、本项目）；接口本轮失败时用来保留上次的结果。"""
    if ctx.db is None or not ids:
        return {}
    from sqlalchemy import select

    from app.models import Resource

    found: dict[str, dict] = {}
    for start in range(0, len(ids), 500):
        for resource_id, attrs in ctx.db.execute(select(Resource.resource_id, Resource.attrs).where(
                Resource.resource_type == resource_type, Resource.tenant_id == ctx.tenant_id,
                Resource.project_ref == ctx.project_ref, Resource.resource_id.in_(ids[start:start + 500]))):
            value = attrs.get(key) if isinstance(attrs, dict) else None
            if isinstance(value, dict):
                found[resource_id] = value
    return found


def log_collection_failure(logger: logging.Logger, ctx: RunContext, exc: Exception, *,
                           cloud_service: str, phase: str, **fields: object) -> None:
    """局部采集失败保留安全调用栈；不记录返回正文、headers、事实或认证上下文。"""
    details: dict[str, object] = {"event": "collection_partial_failure", "phase": phase,
                                  "cloud_service": cloud_service, "tenant_id": ctx.tenant_id,
                                  "project_id": ctx.project_id, "project_ref": ctx.project_ref,
                                  "exception_type": type(exc).__name__, **fields}
    if isinstance(exc, HcsError):
        details.update(upstream_status=exc.status, retryable=exc.retryable)
        if isinstance(exc.code, str) and _SAFE_CODE.fullmatch(exc.code):
            details["upstream_code"] = exc.code
        if isinstance(exc.reason, str) and re.fullmatch(r"[a-z_]{1,32}", exc.reason):
            details["network_reason"] = exc.reason
        if isinstance(exc.request_id, str) and _SAFE_REQUEST_ID.fullmatch(exc.request_id):
            details["upstream_request_id"] = exc.request_id
    logger.warning("collection_partial_failure", extra=details,
                   exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))


class Collector(Protocol):
    code: str
    service: str
    name: str

    def collect(self, ctx: RunContext) -> list[Observation]: ...
