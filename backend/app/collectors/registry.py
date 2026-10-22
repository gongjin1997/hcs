from app.k8s.collector import PodEventCollector  # noqa: E402,I001  容器事件采集与巡检共用流水线

from .base import Collector
from .dataarts import DataArtsBatchCollector, DataArtsRealtimeCollector
from .dcs import DcsCollector
from .ecs import EcsStatusCollector
from .gaussdb import GaussDBCollector
from .mrs import MrsClusterCollector, MrsJobCollector

TASKS: dict[str, Collector] = {c.code: c for c in (
    DataArtsRealtimeCollector(), DataArtsBatchCollector(), MrsClusterCollector(), MrsJobCollector(),
    GaussDBCollector(), DcsCollector(), EcsStatusCollector(), PodEventCollector(),
)}
