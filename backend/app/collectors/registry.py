from app.k8s.collector import PodEventCollector  # noqa: E402,I001  容器事件采集与巡检共用流水线
from app.services.cce_monitoring import CceHealthCollector

from .base import Collector
from .dataarts import DataArtsBatchCollector, DataArtsRealtimeCollector
from .dcs import DcsCollector
from .ecs import EcsStatusCollector
from .ecs_metrics import EcsMetricsCollector, EcsQuotaCollector
from .gaussdb import GaussDBCollector
from .mrs import MrsClusterCollector, MrsJobCollector

TASKS: dict[str, Collector] = {c.code: c for c in (
    DataArtsRealtimeCollector(), DataArtsBatchCollector(), MrsClusterCollector(), MrsJobCollector(),
    GaussDBCollector(), DcsCollector(), EcsStatusCollector(), EcsMetricsCollector(), EcsQuotaCollector(), PodEventCollector(),
    CceHealthCollector(),
)}
