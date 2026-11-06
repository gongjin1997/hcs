import logging
import re
from dataclasses import dataclass

from app.hcs import HcsError
from app.hcs.gaussdb_api import gaussdb_instances_path
from app.hcs.gaussdb_native import volume_usage
from app.hcs.pager import page_all
from app.ledger.fetchers import gaussdb_attrs
from app.models.base import utcnow

from .base import Observation, RunContext, log_collection_failure

log = logging.getLogger(__name__)
SAFE_ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
NATIVE_STATES = {"BUILD", "BUILD_FAILED", "ACTIVE", "FAILED", "MODIFYING", "EXPANDING", "REBOOTING", "UPGRADING",
                 "RESTORING", "SWITCHOVER", "MIGRATING", "BACKING UP", "REBALANCING", "UPGRADE TO BE OBSERVED",
                 "MODIFYING DATABASE PORT", "STORAGE FULL", "REPAIRING", "SHUTDOWN"}


@dataclass
class GaussStorageObservation(Observation):
    """采样状态只供本地历史持久化，不污染规则事实或台账 metrics。"""

    storage_status: str = "UNAVAILABLE"


class GaussDBCollector:
    """原生实例状态和磁盘使用量；CPU/内存性能由独立原生巡检报告展示。"""

    code, service, name = "gaussdb_status", "gaussdb", "GaussDB 实例状态与容量"

    def collect(self, ctx: RunContext) -> list[Observation]:
        insts = list(page_all(lambda p: ctx.client.get("gaussdb", gaussdb_instances_path(), params=p),
                              "instances", total_key="total_count"))
        obs: list[Observation] = []
        native_volume = 0
        for i in insts:
            iid, name, status = i["id"], i["name"], i.get("status")
            known_status = isinstance(status, str) and status in NATIVE_STATES | {"FROZEN"}
            if not known_status:
                ctx.errors.append("GaussDB 实例状态缺失或未识别，本次不判定为正常")
            nodes = [{"name": n.get("name"), "role": n.get("role"), "status": n.get("status")}
                     for n in i.get("nodes", [])]
            attrs = {**gaussdb_attrs(i), "nodes": nodes, "metrics_source": "GAUSSDB"}
            status_observation = GaussStorageObservation("gaussdb_instance", iid, name, "instance_status",
                                   {"status": status if known_status else None},
                                   status_raw=status, attrs=attrs,
                                   metrics={"cpu_usage": None, "mem_usage": None, "disk_usage": None,
                                            "disk_used_gb": None, "disk_total_gb": None}, metric_observed_at=utcnow())
            obs.append(status_observation)
            if status != "ACTIVE":
                continue
            try:
                used, total, usage = volume_usage(ctx.client, iid)
            except (HcsError, ValueError) as exc:
                status_observation.storage_status = "ERROR"
                status_observation.metric_observed_at = utcnow()
                detail = _failure_kind(exc)
                ctx.errors.append(f"GaussDB 原生磁盘用量查询失败（{detail}）")
                log_fields: dict[str, object] = {"failure_kind": detail}
                if isinstance(exc, HcsError):
                    log_fields.update(upstream_status=exc.status, upstream_code=_safe_code(exc.code))
                log_collection_failure(log, ctx, exc, cloud_service="gaussdb", phase="native_volume", **log_fields)
                continue
            native_volume += 1
            m = {"disk_used_gb": used, "disk_total_gb": total}
            if usage is not None:
                m["disk_usage"] = usage
                obs.append(GaussStorageObservation("gaussdb_instance", iid, name, "disk", {"disk_usage": usage}, metrics=m,
                                                   metric_observed_at=utcnow(), storage_status="AVAILABLE"))
            else:
                status_observation.metrics.update(m)
                status_observation.storage_status = "PARTIAL"
                status_observation.metric_observed_at = utcnow()
        ctx.stats.update(metrics_source="GAUSSDB", native_volume=native_volume)
        measured = {o.resource_id for o in obs if o.check_item == "disk"}
        for state in list(obs):
            if (isinstance(state, GaussStorageObservation) and state.check_item == "instance_status"
                    and state.resource_id not in measured):
                obs.append(GaussStorageObservation("gaussdb_instance", state.resource_id, state.name, "disk",
                    {"disk_usage": None}, metrics={key: state.metrics.get(key)
                    for key in ("disk_usage", "disk_used_gb", "disk_total_gb")},
                    metric_observed_at=state.metric_observed_at, storage_status=state.storage_status))
        for observation in obs:
            if not isinstance(observation, GaussStorageObservation):
                continue
            observation.attrs["gaussdb_monitoring"] = {
                "source": "GAUSSDB", "api_version": "v3", "endpoint": "volume-usage",
                "time_basis": "RESPONSE_RECEIVED", "stale_after_sec": 900,
                "observed_at": observation.metric_observed_at.isoformat() if observation.metric_observed_at else None,
                "status": observation.storage_status,
            }
        return obs


def _safe_code(value: str) -> str | None:
    return value if isinstance(value, str) and SAFE_ERROR_CODE.fullmatch(value) else None


def _failure_kind(exc: Exception) -> str:
    if not isinstance(exc, HcsError):
        return type(exc).__name__
    code = _safe_code(exc.code)
    if exc.status:
        return f"HTTP {exc.status}" + (f" / {code}" if code else "")
    return code or "NETWORK_OR_RESPONSE_ERROR"
