import logging

from app.hcs import HcsError
from app.hcs.pager import page_all
from app.metrics.dcs_samples import finite_number, metric_values
from app.models.base import utcnow

from .base import Observation, RunContext, log_collection_failure

log = logging.getLogger(__name__)
NATIVE_STATES = {"CREATING", "CREATEFAILED", "RUNNING", "ERROR", "RESTARTING", "EXTENDING", "RESTORING"}
# 旧部署仍可返回这些告警状态；保留识别，不把未定义的新状态判作健康。
COMPATIBLE_STATES = {"FROZEN", "CLOSED", "UPGRADING"}


class DcsCollector:
    """DCS V2 原生只读统计；CPU 累计秒，禁止跨版本或云监控自动回退。"""

    code, service, name = "dcs_status", "dcs", "DCS 实例状态与内存"

    def collect(self, ctx: RunContext) -> list[Observation]:
        list_times = {}

        def page(params):
            body = ctx.client.get("dcs", "/v2/{project_id}/instances", params=params)
            received = utcnow()
            values = body.get("instances")
            for instance in values if isinstance(values, list) else []:
                if isinstance(instance, dict):
                    list_times[instance.get("instance_id")] = received
            return body

        insts = list(page_all(page, "instances", limit=1000, total_key="instance_num"))
        statistics: dict[str, dict] = {}
        statistic_at = None
        warning = None
        if any(i.get("status") == "RUNNING" for i in insts):
            try:
                body = ctx.client.get("dcs", "/v2/{project_id}/instances/statistic")
                statistic_at = utcnow()
                values = body.get("statistics") if isinstance(body, dict) else None
                if not isinstance(values, list):
                    raise HcsError(0, "LOCAL.DCS_RESPONSE", "DCS 统计列表结构不完整")
                for item in values:
                    if not isinstance(item, dict) or not isinstance(item.get("instance_id"), str):
                        raise HcsError(0, "LOCAL.DCS_RESPONSE", "DCS 统计实例标识无效")
                    if item["instance_id"] in statistics:
                        raise HcsError(0, "LOCAL.DCS_RESPONSE", "DCS 统计实例标识重复")
                    statistics[item["instance_id"]] = item
            except HcsError as exc:
                ctx.errors.append(f"DCS 当前统计：{exc}")
                log_collection_failure(log, ctx, exc, cloud_service="dcs", phase="native_statistics")
                statistics.clear()
                warning = "当前统计不可用，仅保留本次实例列表中的内存字段"
        policy = ctx.params.get("_collection_policy") or {}
        stale_after = policy.get("stale_after_sec", 900) if isinstance(policy, dict) else 900
        if isinstance(stale_after, bool) or not isinstance(stale_after, int) or stale_after < 60:
            stale_after = 900
        ctx.stats.update(metrics_source="DCS", native_statistics=len(statistics), statistics_api_version="v2")
        obs: list[Observation] = []
        for i in insts:
            iid, name, status = i["instance_id"], i["name"], i.get("status")
            known_status = isinstance(status, str) and status in NATIVE_STATES | COMPATIBLE_STATES
            if not known_status:
                ctx.errors.append("DCS 实例状态缺失或未识别，本次不判定为正常")
            capacity = finite_number(i.get("capacity"))
            if capacity is None or capacity == 0:
                minor = finite_number(i.get("capacity_minor"))
                capacity = minor if minor is not None and minor > 0 else None
            attrs = {k: i.get(k) for k in ("engine", "engine_version", "spec_code", "ip", "port",
                                           "max_memory", "max_clients")}
            attrs["capacity"] = capacity
            native = statistics.get(iid) if status == "RUNNING" else None
            current = native if native is not None else i
            # 缺字段保持 NULL，不能混合同一次统计与另一次列表的分子分母。
            raw = {"used_memory_mb": current.get("used_memory"), "max_memory_mb": current.get("max_memory")}
            if native is not None:
                raw.update({key: native.get(key) for key in ("input_kbps", "output_kbps", "keys",
                                                            "cmd_get_count", "cmd_set_count")})
                raw["used_cpu_seconds"] = native.get("used_cpu")
            metrics = metric_values(raw)
            used, total = metrics["used_memory_mb"], metrics["max_memory_mb"]
            if used is not None and total is not None:
                usage = finite_number(used / total * 100)
                metrics["memory_usage"] = round(usage, 1) if usage is not None else None
            at = statistic_at if native is not None else list_times.get(iid)
            message = warning if native is None else None
            if native is None and status == "RUNNING" and not message:
                message = "统计接口未返回此实例，仅保留本次实例列表中的内存字段"
            attrs.update(metrics_source="DCS", dcs_monitoring={
                "source": "DCS", "api_version": "v2", "endpoint": "instances/statistic" if native is not None else "instances",
                "observed_at": at.isoformat() if at else None, "time_basis": "RESPONSE_RECEIVED",
                "stale_after_sec": stale_after, "warning": message,
            })
            obs.append(Observation("dcs_instance", iid, name, "instance_status", {"status": status if known_status else None},
                status_raw=status, attrs=attrs, metrics={**metrics, "cpu_usage": None, "connected_clients": None},
                metric_observed_at=at))
            # 无值也送入规则引擎：中断连续命中，但不恢复已有告警。
            memory = {key: metrics[key] for key in ("memory_usage", "used_memory_mb", "max_memory_mb")}
            obs.append(Observation("dcs_instance", iid, name, "memory", memory, metrics=memory, metric_observed_at=at))
        return obs
