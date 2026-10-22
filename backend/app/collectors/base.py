from dataclasses import dataclass, field
from typing import Any, Protocol

from app.hcs import HcsClient


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


class Collector(Protocol):
    code: str
    service: str
    name: str

    def collect(self, ctx: RunContext) -> list[Observation]: ...
