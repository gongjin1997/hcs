import hashlib
import json
from datetime import datetime

from sqlalchemy import Boolean, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JsonType, UtcDateTime

from .base import utcnow

SCOPE_META = "_pod_scope_0007"


def pod_state_key(tenant_id: int | None, project_ref: int | None, cluster_id: str, namespace: str, pod: str) -> str:
    """仅用于数据库主键；公开定位仍使用 cluster_id/namespace/pod。"""
    value = json.dumps([tenant_id, project_ref, cluster_id, namespace, pod], ensure_ascii=True, separators=(",", ":"))
    return "pod:" + hashlib.sha256(value.encode()).hexdigest()


def pod_event_key(tenant_id: int | None, project_ref: int | None, cluster_id: str, dedup: str) -> str:
    value = json.dumps([tenant_id, project_ref, cluster_id, dedup], ensure_ascii=True, separators=(",", ":"))
    return "event:" + hashlib.sha256(value.encode()).hexdigest()


class PodEvent(Base):
    """CCE Pod 事件：重启 / CrashLoopBackOff / 背压 / 驱逐 / 探针失败 / 调度失败等，来源是 K8s Event、Pod 状态比对和背压探针。

    持续型事件（背压、CrashLoopBackOff）有开始和结束：ongoing=True 表示仍在持续，结束后写 end_time。
    """

    __tablename__ = "pod_event"
    __table_args__ = (
        Index("idx_pod_event_tenant_time", "tenant_id", "last_time"),
        Index("idx_pod_event_pod", "cluster_id", "namespace", "pod"),
        Index("idx_pod_event_type_time", "event_type", "last_time"),
        Index("idx_pod_event_scope_dedup", "tenant_id", "project_ref", "cluster_id", "dedup_key"),
        Index("idx_pod_event_scope_pod", "tenant_id", "project_ref", "cluster_id", "namespace", "pod"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer)
    project_ref: Mapped[int | None] = mapped_column(Integer)
    cluster_id: Mapped[str] = mapped_column(String(64))
    cluster_name: Mapped[str] = mapped_column(String(128))
    namespace: Mapped[str] = mapped_column(String(128))
    workload_kind: Mapped[str] = mapped_column(String(32), default="")       # Deployment / StatefulSet / DaemonSet / Job
    workload: Mapped[str] = mapped_column(String(255), default="")
    pod: Mapped[str] = mapped_column(String(255), default="")
    container: Mapped[str] = mapped_column(String(128), default="")
    node: Mapped[str | None] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(24))       # 见 app.k8s.catalog.EVENT_TYPES
    level: Mapped[str] = mapped_column(String(8))             # CRITICAL / WARNING / INFO
    reason: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(16))           # K8S_EVENT / POD_STATUS / PROBE
    count: Mapped[int] = mapped_column(Integer, default=1)
    first_time: Mapped[datetime] = mapped_column(UtcDateTime())
    last_time: Mapped[datetime] = mapped_column(UtcDateTime())
    end_time: Mapped[datetime | None] = mapped_column(UtcDateTime())
    ongoing: Mapped[bool] = mapped_column(Boolean, default=False)
    peak: Mapped[float | None] = mapped_column(Float)         # 背压探针的峰值（例如 860 ms/s）
    detail: Mapped[dict] = mapped_column(JsonType, default=dict)
    dedup_key: Mapped[str] = mapped_column(String(160), index=True)   # scope hash，原始去重键保存在 detail 元数据中
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class PodState(Base):
    """Pod 最新状态快照：用于比对重启次数、跟踪持续型事件，也是“Pod 时间线”抽屉的基本信息。"""

    __tablename__ = "pod_state"
    __table_args__ = (Index("idx_pod_state_tenant", "tenant_id", "cluster_id"),
                      Index("idx_pod_state_scope_pod", "tenant_id", "project_ref", "cluster_id", "namespace", "pod"))
    key: Mapped[str] = mapped_column(String(320), primary_key=True)   # pod_state_key，包含 tenant/project 作用域
    tenant_id: Mapped[int | None] = mapped_column(Integer)
    project_ref: Mapped[int | None] = mapped_column(Integer)
    cluster_id: Mapped[str] = mapped_column(String(64))
    cluster_name: Mapped[str] = mapped_column(String(128))
    namespace: Mapped[str] = mapped_column(String(128))
    pod: Mapped[str] = mapped_column(String(255))
    workload_kind: Mapped[str] = mapped_column(String(32), default="")
    workload: Mapped[str] = mapped_column(String(255), default="")
    node: Mapped[str | None] = mapped_column(String(128))
    pod_ip: Mapped[str | None] = mapped_column(String(64))
    phase: Mapped[str] = mapped_column(String(32), default="")
    ready: Mapped[bool] = mapped_column(Boolean, default=False)
    status_reason: Mapped[str | None] = mapped_column(String(64))     # CrashLoopBackOff / ImagePullBackOff / Evicted ...
    restarts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    labels: Mapped[dict] = mapped_column(JsonType, default=dict)
    # [{name, image, restart_count, ready, state, last_reason, last_exit_code, last_finished}]
    containers: Mapped[list] = mapped_column(JsonType, default=list)
    data: Mapped[dict] = mapped_column(JsonType, default=dict)        # 持续型事件跟踪：{"bp": {...}, "crash": {...}}
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class PodProbe(Base):
    """背压探针：不引入 Prometheus，按需经 kube-apiserver 代理抓取 Pod /metrics 中的一个指标，超过阈值持续 N 分钟记为“背压”。"""

    __tablename__ = "pod_probe"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(64))
    cluster_id: Mapped[str] = mapped_column(String(64), default="*")      # * = 该租户全部集群
    namespace: Mapped[str] = mapped_column(String(128))
    selector: Mapped[str] = mapped_column(String(255), default="")        # 标签选择器，例如 component=taskmanager
    port: Mapped[int] = mapped_column(Integer, default=9249)
    path: Mapped[str] = mapped_column(String(128), default="/metrics")
    metric: Mapped[str] = mapped_column(String(160))                     # Prometheus 文本格式中的指标名
    label_filter: Mapped[str] = mapped_column(String(255), default="")    # 可选：只看带这些标签的序列，例如 job_name=rt_order
    agg: Mapped[str] = mapped_column(String(8), default="max")           # max / avg / sum（同一 Pod 多条序列的聚合）
    op: Mapped[str] = mapped_column(String(4), default=">=")
    threshold: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(16), default="")
    for_minutes: Mapped[int] = mapped_column(Integer, default=3)          # 持续超过阈值 N 分钟才算背压开始
    recover_minutes: Mapped[int] = mapped_column(Integer, default=5)      # 连续低于阈值 N 分钟算背压结束
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_result: Mapped[dict] = mapped_column(JsonType, default=dict)     # {at, pods, hit, max, errors}
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow, onupdate=utcnow)
