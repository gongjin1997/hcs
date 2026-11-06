import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from app.hcs import HcsError
from app.hcs.gaussdb_api import gaussdb_instances_path
from app.hcs.gaussdb_native import COMPONENT_STATES, components, volume_usage
from app.hcs.pager import page_all
from app.ledger.fetchers import gaussdb_attrs
from app.models.base import utcnow

from .base import Observation, RunContext, log_collection_failure, previous_attrs

log = logging.getLogger(__name__)
SAFE_ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
NATIVE_STATES = {"BUILD", "BUILD_FAILED", "ACTIVE", "FAILED", "MODIFYING", "EXPANDING", "REBOOTING", "UPGRADING",
                 "RESTORING", "SWITCHOVER", "MIGRATING", "BACKING UP", "REBALANCING", "UPGRADE TO BE OBSERVED",
                 "MODIFYING DATABASE PORT", "STORAGE FULL", "REPAIRING", "SHUTDOWN"}
# 实例在线、可以查询磁盘用量与组件的状态：正常、磁盘满（正是最需要看用量的时候）、备份中
ONLINE_STATES = {"ACTIVE", "STORAGE FULL", "BACKING UP"}
MAX_LISTED_COMPONENTS = 5


@dataclass
class GaussStorageObservation(Observation):
    """采样状态只供本地历史持久化，不污染规则事实或台账 metrics。"""

    storage_status: str = "UNAVAILABLE"


def component_summary(items: list[dict], observed_at: datetime) -> tuple[dict, dict]:
    """组件汇总（attrs.gaussdb_components）与 components 检查项事实。Down 为宕机；状态不在表 4-80 中的计为未识别。"""
    down = [item for item in items if item["status"] == "Down"]
    labels = [f"{item['component_type']} {item['component_id']}（{item['node_name'] or item['node_id']}）" for item in down]
    listed = "、".join(labels[:MAX_LISTED_COMPONENTS]) + (
        f" 等 {len(labels)} 个" if len(labels) > MAX_LISTED_COMPONENTS else "")
    unknown = sum(item["status"] not in COMPONENT_STATES for item in items)
    summary = {"state": "AVAILABLE" if items else "NO_DATA", "observed_at": observed_at.isoformat(),
               "warning": "部分组件状态未识别，组件告警暂不恢复" if unknown else None,
               "total": len(items), "by_type": dict(Counter(item["component_type"] for item in items)),
               "down": len(down), "unknown": unknown, "items": items}
    return summary, {"down_components": len(down), "unknown_components": unknown, "down_list": listed}


class GaussDBCollector:
    """原生实例状态、磁盘使用量与组件状态；CPU/内存性能由独立原生巡检报告展示。"""

    code, service, name = "gaussdb_status", "gaussdb", "GaussDB 实例状态与容量"

    def collect(self, ctx: RunContext) -> list[Observation]:
        insts = list(page_all(lambda p: ctx.client.get("gaussdb", gaussdb_instances_path(), params=p),
                              "instances", total_key="total_count"))
        scope = ctx.scope("gaussdb_instance")      # 采集范围：范围外的实例不再逐个查询磁盘用量与组件
        if not scope.open:
            insts = [i for i in insts if scope.keep({"id": i["id"], "name": i["name"]})]
        obs: list[Observation] = []
        native_volume = native_components = 0
        failed_components: list[GaussStorageObservation] = []
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
            if status not in ONLINE_STATES:
                continue
            if self._volume(ctx, status_observation, obs):
                native_volume += 1
            try:
                items = components(ctx.client, iid)
            except (HcsError, ValueError) as exc:
                detail = _failure_kind(exc)
                ctx.errors.append(f"GaussDB 组件状态查询失败（{detail}）")
                log_collection_failure(log, ctx, exc, cloud_service="gaussdb", phase="native_components",
                                       failure_kind=detail)
                failed_components.append(status_observation)
                continue
            native_components += 1
            summary, facts = component_summary(items, utcnow())
            attrs["gaussdb_components"] = summary
            if items:       # 空清单不是“组件全部正常”的证据：按缺采处理，不恢复告警
                obs.append(Observation("gaussdb_instance", iid, name, "components", facts,
                                       metric_observed_at=utcnow()))
        if failed_components:   # 查询失败：保留上次的组件清单并标注，不能让旧结果冒充本轮
            previous = previous_attrs(ctx, "gaussdb_instance", [o.resource_id for o in failed_components],
                                      "gaussdb_components")
            for failed in failed_components:
                last = previous.get(failed.resource_id) or {}
                stale = last.get("state") in ("AVAILABLE", "STALE") and isinstance(last.get("observed_at"), str)
                failed.attrs["gaussdb_components"] = (
                    {**last, "state": "STALE", "warning": "本轮组件查询失败，显示的是上次获取的组件"} if stale else
                    {"state": "ERROR", "observed_at": None, "warning": "组件查询失败"})
        ctx.stats.update(metrics_source="GAUSSDB", native_volume=native_volume, native_components=native_components)
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

    @staticmethod
    def _volume(ctx: RunContext, status_observation: "GaussStorageObservation", obs: list[Observation]) -> bool:
        """§4.2.1 原生磁盘用量；成功时追加 disk 观测，失败只记局部错误（组件查询照常进行）。"""
        iid, name = status_observation.resource_id, status_observation.name
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
            return False
        m = {"disk_used_gb": used, "disk_total_gb": total}
        if usage is not None:
            m["disk_usage"] = usage
            obs.append(GaussStorageObservation("gaussdb_instance", iid, name, "disk", {"disk_usage": usage}, metrics=m,
                                               metric_observed_at=utcnow(), storage_status="AVAILABLE"))
        else:
            status_observation.metrics.update(m)
            status_observation.storage_status = "PARTIAL"
            status_observation.metric_observed_at = utcnow()
        return True


def _safe_code(value: str) -> str | None:
    return value if isinstance(value, str) and SAFE_ERROR_CODE.fullmatch(value) else None


def _failure_kind(exc: Exception) -> str:
    if not isinstance(exc, HcsError):
        return type(exc).__name__
    code = _safe_code(exc.code)
    if exc.status:
        return f"HTTP {exc.status}" + (f" / {code}" if code else "")
    return code or "NETWORK_OR_RESPONSE_ERROR"
