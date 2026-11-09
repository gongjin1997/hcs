"""巡检任务代码 → 采集器。任务定义（名称、周期、资源类型、检查项）统一在 app.services.catalog.TASK_SPECS。"""
from app.k8s.collector import PodEventCollector  # noqa: E402,I001  容器事件采集与巡检共用流水线
from app.services.catalog import COLLECTOR_TASK_CODES
from app.services.cce_monitoring import CceHealthCollector

from .base import Collector
from .dataarts import DataArtsBatchCollector, DataArtsRealtimeCollector
from .dcs import DcsCollector
from .ecs import EcsStatusCollector
from .ecs_metrics import EcsMetricsCollector, EcsQuotaCollector
from .gaussdb import GaussDBCollector, GaussDBComponentsCollector, GaussDBStorageCollector
from .mrs import MrsClusterCollector, MrsJobCollector

TASKS: dict[str, Collector] = {c.code: c for c in (
    DataArtsRealtimeCollector(), DataArtsBatchCollector(), MrsClusterCollector(), MrsJobCollector(),
    GaussDBCollector(), GaussDBStorageCollector(), GaussDBComponentsCollector(), DcsCollector(),
    EcsStatusCollector(), EcsMetricsCollector(), EcsQuotaCollector(), PodEventCollector(),
    CceHealthCollector(),
)}

if set(TASKS) != set(COLLECTOR_TASK_CODES):  # 启动即失败，避免任务目录与采集器各自增减
    raise RuntimeError(f"采集器与 catalog.TASK_SPECS 不一致：{sorted(set(TASKS) ^ set(COLLECTOR_TASK_CODES))}")
