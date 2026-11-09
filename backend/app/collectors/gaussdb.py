import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any

from app.core.logging import log_context
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


@dataclass
class _Detail:
    value: Any
    error: HcsError | ValueError | None
    observed_at: datetime
    duration_ms: float


def _instances(ctx: RunContext) -> tuple[list[dict], float]:
    started = perf_counter()
    items = list(page_all(lambda p: ctx.client.get("gaussdb", gaussdb_instances_path(), params=p),
                          "instances", total_key="total_count"))
    scope = ctx.scope("gaussdb_instance")
    if not scope.open:
        items = [item for item in items if scope.keep({"id": item["id"], "name": item["name"]})]
    return items, round((perf_counter() - started) * 1000, 2)


def _online(item: dict) -> bool:
    status = item.get("status")
    return isinstance(status, str) and status in ONLINE_STATES


def _stats(ctx: RunContext, phase: str, items: list[dict], started: float, list_ms: float,
           results: dict[str, _Detail] | None = None, *, concurrency: int = 0, detail_ms: float = 0) -> None:
    results = results or {}
    durations = [item.duration_ms for item in results.values()]
    summary = {"phase": phase, "instances": len(items),
               "online": sum(_online(item) for item in items),
               "queried": len(results), "failed": sum(item.error is not None for item in results.values()),
               "concurrency": concurrency, "list_ms": list_ms, "detail_ms": round(detail_ms, 2),
               "detail_total_ms": round(sum(durations), 2), "detail_max_ms": round(max(durations, default=0), 2),
               "wall_ms": round((perf_counter() - started) * 1000, 2)}
    ctx.stats.update(metrics_source="GAUSSDB", gaussdb=summary)
    log.info("gaussdb_collection_finished", extra={"event": "gaussdb_collection_finished", **summary})


def _details(ctx: RunContext, items: list[dict], phase: str) -> tuple[dict[str, _Detail], int, float]:
    selected = [item for item in items if _online(item)]
    if not selected:
        return {}, 0, 0
    # 复用客户端的全局/服务 QPS、在途上限与重试预算，不创建额外客户端绕过限流。
    concurrency = min(len(selected), max(1, min(16, int(ctx.params.get("hcs_concurrency", 4)))),
                      max(1, int(getattr(ctx.client, "max_concurrency", 2))))

    def fetch(item: dict) -> _Detail:
        started = perf_counter()
        with log_context(collection_phase=phase, resource_id=item["id"]):
            value, error = None, None
            try:
                value = volume_usage(ctx.client, item["id"]) if phase == "storage" else components(ctx.client, item["id"])
            except (HcsError, ValueError) as exc:
                error = exc
                log_collection_failure(log, ctx, exc, cloud_service="gaussdb", phase=phase,
                                       resource_id=item["id"], failure_kind=_failure_kind(exc))
            elapsed = (perf_counter() - started) * 1000
            log.info("gaussdb_detail_finished", extra={"event": "gaussdb_detail_finished", "phase": phase,
                     "resource_id": item["id"], "duration_ms": round(elapsed, 2),
                     "result": "ERROR" if error else "SUCCESS"})
            return _Detail(value, error, utcnow(), elapsed)

    started = perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix=f"gaussdb-{phase}") as pool:
        futures = [pool.submit(copy_context().run, fetch, item) for item in selected]
        results = {item["id"]: future.result() for item, future in zip(selected, futures, strict=True)}
    return results, concurrency, (perf_counter() - started) * 1000


class GaussDBCollector:
    """实例状态独立检查，不等待磁盘/组件接口，也不覆盖它们的采样结果。"""

    code, service, name = "gaussdb_status", "gaussdb", "GaussDB 实例状态"

    def collect(self, ctx: RunContext) -> list[Observation]:
        started = perf_counter()
        items, list_ms = _instances(ctx)
        observations = []
        for item in items:
            status = item.get("status")
            known = isinstance(status, str) and status in NATIVE_STATES | {"FROZEN"}
            if not known:
                ctx.errors.append("GaussDB 实例状态缺失或未识别，本次不判定为正常")
            nodes = [{"name": n.get("name"), "role": n.get("role"), "status": n.get("status")}
                     for n in item.get("nodes", [])]
            observations.append(Observation("gaussdb_instance", item["id"], item["name"], "instance_status",
                {"status": status if known else None}, status_raw=status if isinstance(status, str) else None,
                attrs={**gaussdb_attrs(item), "nodes": nodes, "metrics_source": "GAUSSDB"},
                metrics={"cpu_usage": None, "mem_usage": None}))
        _stats(ctx, "status", items, started, list_ms)
        return observations


class GaussDBStorageCollector:
    """磁盘用量独立采集；失败/停机记缺采，不使用配置容量补造使用率。"""

    code, service, name = "gaussdb_storage", "gaussdb", "GaussDB 磁盘容量"

    def collect(self, ctx: RunContext) -> list[Observation]:
        started = perf_counter()
        items, list_ms = _instances(ctx)
        results, concurrency, detail_ms = _details(ctx, items, "storage")
        observations: list[Observation] = []
        for item in items:
            result = results.get(item["id"])
            status, used, total, usage = "UNAVAILABLE", None, None, None
            if result is not None:
                if result.error:
                    status = "ERROR"
                    ctx.errors.append(f"GaussDB 原生磁盘用量查询失败（{_failure_kind(result.error)}）")
                else:
                    used, total, usage = result.value
                    status = "AVAILABLE" if usage is not None else "PARTIAL"
            at = result.observed_at if result is not None else utcnow()
            observations.append(GaussStorageObservation("gaussdb_instance", item["id"], item["name"], "disk",
                {"disk_usage": usage}, metrics={"disk_usage": usage, "disk_used_gb": used, "disk_total_gb": total},
                metric_observed_at=at, storage_status=status,
                attrs={"metrics_source": "GAUSSDB", "gaussdb_monitoring": {
                    "source": "GAUSSDB", "api_version": "v3", "endpoint": "volume-usage",
                    "time_basis": "RESPONSE_RECEIVED", "stale_after_sec": ctx.params.get("_inspection_stale_after_sec", 900),
                    "observed_at": at.isoformat(), "status": status}}))
        ctx.stats["native_volume"] = sum(item.error is None for item in results.values())
        _stats(ctx, "storage", items, started, list_ms, results, concurrency=concurrency, detail_ms=detail_ms)
        return observations


class GaussDBComponentsCollector:
    """组件单独采集；空清单/未识别/失败不当作正常，不恢复告警。"""

    code, service, name = "gaussdb_components", "gaussdb", "GaussDB 组件健康"

    def collect(self, ctx: RunContext) -> list[Observation]:
        started = perf_counter()
        items, list_ms = _instances(ctx)
        results, concurrency, detail_ms = _details(ctx, items, "components")
        failed = [iid for iid, result in results.items() if result.error is not None]
        previous = previous_attrs(ctx, "gaussdb_instance", failed, "gaussdb_components")
        observations = []
        for item in items:
            result = results.get(item["id"])
            facts: dict[str, Any] = {"down_components": None, "unknown_components": None, "down_list": ""}
            attrs: dict[str, Any] = {}
            if result is not None:
                if result.error:
                    ctx.errors.append(f"GaussDB 组件状态查询失败（{_failure_kind(result.error)}）")
                    last = previous.get(item["id"]) or {}
                    stale = last.get("state") in ("AVAILABLE", "STALE") and isinstance(last.get("observed_at"), str)
                    attrs["gaussdb_components"] = (
                        {**last, "state": "STALE", "warning": "本轮组件查询失败，显示的是上次获取的组件"} if stale else
                        {"state": "ERROR", "observed_at": None, "warning": "组件查询失败"})
                else:
                    summary, measured = component_summary(result.value, result.observed_at)
                    attrs["gaussdb_components"] = summary
                    if result.value:
                        facts = measured
            observations.append(Observation("gaussdb_instance", item["id"], item["name"], "components", facts,
                attrs=attrs, metric_observed_at=result.observed_at if result is not None else None))
        ctx.stats["native_components"] = sum(item.error is None for item in results.values())
        _stats(ctx, "components", items, started, list_ms, results, concurrency=concurrency, detail_ms=detail_ms)
        return observations


def _safe_code(value: str) -> str | None:
    return value if isinstance(value, str) and SAFE_ERROR_CODE.fullmatch(value) else None


def _failure_kind(exc: Exception) -> str:
    if not isinstance(exc, HcsError):
        return type(exc).__name__
    code = _safe_code(exc.code)
    if exc.status:
        return f"HTTP {exc.status}" + (f" / {code}" if code else "")
    return code or "NETWORK_OR_RESPONSE_ERROR"
