import logging
import re
from datetime import datetime

from app.hcs import HcsError
from app.hcs.pager import page_all
from app.metrics.dcs_samples import finite_number, metric_values, parse_observed_at
from app.models.base import utcnow

from .base import Observation, RunContext, log_collection_failure, previous_attrs

log = logging.getLogger(__name__)
NATIVE_STATES = {"CREATING", "CREATEFAILED", "RUNNING", "ERROR", "RESTARTING", "EXTENDING", "RESTORING"}
# 旧部署仍可返回这些告警状态；保留识别，不把未定义的新状态判作健康。
COMPATIBLE_STATES = {"FROZEN", "CLOSED", "UPGRADING"}
CPU_MIN_GAP_SEC = 20          # 两次累计值间隔太短，秒级抖动会放大误差
CPU_MAX_GAP_SEC = 1800        # 间隔过长（采集中断、任务停过）不能代表当前负载

# 逻辑节点（分片 / 主从 / Proxy）：DCS API §4.3.3 批量查询实例节点信息，表 4-109 的枚举
NODES_PATH = "/v2/{project_id}/instances-logical-nodes"
NODE_STATES = {"Creating", "Active", "Inactive", "Deleting", "AddSharding"}
NODE_TRANSITIONAL = {"Creating", "Deleting", "AddSharding"}
NODE_ROLES = {"redis-server", "redis-proxy"}
# 创建中 / 创建失败的实例没有节点（§4.3.3 “创建中实例不返回节点信息”）；状态未识别的实例不为它单独发请求
NODE_QUERY_STATES = (NATIVE_STATES | COMPATIBLE_STATES) - {"CREATING", "CREATEFAILED"}
NODE_ROLE_TEXT = {"master": "主", "slave": "从", "proxy": "Proxy"}
MAX_LISTED_NODES = 5


def master_count(instance: dict) -> int | None:
    """实例详情（§4.1.4）里的 cache_mode / sharding_count 折算主节点数，作为节点接口拿不到时的备用口径。
    单机 / 主备 / 读写分离只有一个主节点；Cluster / Proxy 集群取 sharding_count。
    注意实例列表（§4.1.2）不返回这两个字段，所以平时以逻辑节点里的主节点数为准，见 node_summary。"""
    mode, shards = instance.get("cache_mode"), instance.get("sharding_count")
    if mode in ("single", "ha", "ha_rw_split"):
        return 1
    return shards if mode in ("cluster", "proxy") and type(shards) is int and shards > 0 else None


def nodes_supported(instance: dict) -> bool | None:
    """§4.3.3 只支持 Redis 4.0 及以上；Memcached、Redis 3.0 没有逻辑节点。引擎或版本拿不准时返回 None。"""
    engine, version = instance.get("engine"), instance.get("engine_version")
    if not isinstance(engine, str):
        return None
    if engine.strip().lower() == "memcached":
        return False
    found = re.match(r"\s*(\d+)", version) if engine.strip().lower() == "redis" and isinstance(version, str) else None
    return int(found.group(1)) >= 4 if found else None


def _text(value: object, limit: int = 128) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    return value.strip()[:limit] or None


def node_record(raw: dict) -> dict:
    """节点只保留展示与判定需要的字段。name 用“IP:端口”：主从倒换后节点名前缀会变，IP 与端口不变。"""
    ip, port = _text(raw.get("node_ip"), 64), _text(raw.get("node_port"), 16)
    role, kind = _text(raw.get("node_type"), 16), _text(raw.get("node_role"), 16)
    if kind not in NODE_ROLES:     # node_role 缺失时按 node_type 推断：proxy 节点角色固定为 redis-proxy（表 4-109）
        kind = "redis-proxy" if role == "proxy" else "redis-server" if role in ("master", "slave") else None
    label = (f"{ip}:{port}" if ip and port else ip) or _text(raw.get("name")) or _text(raw.get("logical_node_id"))
    return {"name": label or "-", "node_name": _text(raw.get("name")), "role": role, "kind": kind,
            "status": _text(raw.get("status"), 32), "shard": _text(raw.get("group_name")),
            "shard_id": _text(raw.get("group_id")), "ip": ip, "port": port, "az": _text(raw.get("az_code"), 64)}


def logical_nodes(ctx: RunContext) -> dict[str, list[dict]]:
    """一次分页遍历取本项目全部实例的逻辑节点（每页最多 100 个实例，count 为实例总数）。结构不对整批作废，不用半份结果。"""
    found: dict[str, list[dict]] = {}
    pages = page_all(lambda params: ctx.client.get("dcs", NODES_PATH, params=params), "instances",
                     limit=100, total_key="count")
    for item in pages:
        iid, nodes = item.get("instance_id"), item.get("nodes")
        if not isinstance(iid, str) or not iid or iid in found:
            raise HcsError(0, "LOCAL.DCS_RESPONSE", "DCS 节点列表的实例标识缺失或重复")
        if nodes is None:
            nodes = []
        if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
            raise HcsError(0, "LOCAL.DCS_RESPONSE", "DCS 节点列表结构不完整")
        found[iid] = [node_record(node) for node in nodes]
    return found


def node_summary(nodes: list[dict]) -> dict:
    """按主节点 / 从节点 / Proxy 汇总；状态未识别的节点单独计数，不当作正常。"""
    servers = [n for n in nodes if n["kind"] == "redis-server"]
    shards = {n["shard_id"] or n["shard"] for n in servers if n["shard_id"] or n["shard"]}
    inactive = [n for n in nodes if n["status"] == "Inactive"]
    inactive.sort(key=lambda n: (n["role"] != "master", n["shard"] or "", n["name"]))
    labels = [" ".join(part for part in (n["shard"], NODE_ROLE_TEXT.get(n["role"] or "", "节点"), n["name"]) if part)
              for n in inactive]
    listed = "、".join(labels[:MAX_LISTED_NODES]) + (f" 等 {len(labels)} 个" if len(labels) > MAX_LISTED_NODES else "")
    return {"node_count": len(nodes), "shards": len(shards) or None,
            "masters": sum(n["role"] == "master" for n in servers),
            "replicas": sum(n["role"] == "slave" for n in servers),
            "proxies": sum(n["kind"] == "redis-proxy" for n in nodes),
            "inactive": len(inactive),
            "inactive_masters": sum(n["role"] == "master" and n["kind"] == "redis-server" for n in inactive),
            "transitional": sum(n["status"] in NODE_TRANSITIONAL for n in nodes),
            "unknown": sum(n["status"] not in NODE_STATES for n in nodes), "inactive_list": listed}


def instance_nodes(instance: dict, status: str | None, fetched: dict[str, list[dict]] | None,
                   fetched_at: datetime | None, failed: bool, previous: object) -> tuple[dict, list[dict] | None, dict | None]:
    """一个实例的节点结果：(attrs.dcs_topology 汇总, attrs.nodes 清单, nodes 检查项事实)。

    清单为 None 表示本轮不改写已保存的清单（接口失败）；事实为 None 表示没有可判定的证据（按缺采处理，不恢复告警）。
    Memcached / Redis 3.0 没有逻辑节点，按“不适用”给出 0，避免这类实例永远处于“部分覆盖”。"""
    iid = instance["instance_id"]
    observed = fetched_at.isoformat() if fetched_at else None
    nodes = fetched.get(iid) if fetched is not None else None
    if nodes:
        summary = node_summary(nodes)
        listed = summary.pop("inactive_list")
        warning = ("部分节点状态未识别，节点告警暂不恢复" if summary["unknown"] else
                   "主节点故障" if summary["inactive_masters"] else "有节点故障" if summary["inactive"] else None)
        facts = {"instance_status": status, "inactive_masters": summary["inactive_masters"],
                 "inactive_other_nodes": summary["inactive"] - summary["inactive_masters"],
                 "unknown_nodes": summary["unknown"], "inactive_list": listed}
        return {"state": "AVAILABLE", "observed_at": observed, "warning": warning, **summary}, nodes, facts
    if nodes_supported(instance) is False:
        facts = {"instance_status": status, "inactive_masters": 0, "inactive_other_nodes": 0, "unknown_nodes": 0,
                 "inactive_list": ""}
        return {"state": "NOT_SUPPORTED", "observed_at": observed,
                "warning": "Memcached 与 Redis 3.0 实例不提供逻辑节点查询"}, [], facts
    if fetched is not None:
        creating = instance.get("status") in ("CREATING", "CREATEFAILED")
        return {"state": "NOT_RETURNED", "observed_at": observed,
                "warning": "实例未创建完成，源端不返回节点" if creating else "源端未返回此实例的节点"}, [], None
    if failed:
        stale = (isinstance(previous, dict) and previous.get("state") in ("AVAILABLE", "STALE")
                 and isinstance(previous.get("observed_at"), str))
        if stale and isinstance(previous, dict):
            return {**previous, "state": "STALE", "warning": "本轮节点接口调用失败，显示的是上次获取的节点"}, None, None
        return {"state": "UNAVAILABLE", "observed_at": None, "warning": "节点接口调用失败"}, None, None
    keep = instance.get("status") not in ("CREATING", "CREATEFAILED")
    return {"state": "NOT_QUERIED", "observed_at": None, "warning": "实例状态未就绪，本轮未查询节点"}, \
        (None if keep else []), None


def cpu_masters(instance: dict, topology: dict) -> int | None:
    """折算 CPU 用的主节点数：本轮逻辑节点 → 实例返回的 cache_mode（详情口径）→ 接口失败时上次获取的节点汇总。"""
    def positive(value: object) -> int | None:
        return value if type(value) is int and value > 0 else None

    current = positive(topology.get("masters")) if topology.get("state") == "AVAILABLE" else None
    stale = positive(topology.get("masters")) if topology.get("state") == "STALE" else None
    return current or master_count(instance) or stale


def cpu_rate(previous: object, seconds: float | None, at: datetime | None, masters: int | None) -> float | None:
    """由两次累计 CPU 秒数折算每个主节点的 CPU 占用（%）：Δ秒 ÷ Δ时间 ÷ 主节点数 × 100。

    used_cpu 是进程累计的用户态 + 内核态 CPU 秒数（DCS API §4.2.6），本身不是百分比；两次采样的差值除以间隔才是
    “每秒用了多少个核”。Redis 命令在单线程执行，所以近似反映主线程的饱和程度。计数器回退（实例重启）、
    间隔过短或过长、拿不到主节点数时不输出，不补零、不沿用旧值。"""
    if not isinstance(previous, dict) or seconds is None or at is None or not masters:
        return None
    before_seconds, before_at = previous.get("seconds"), parse_observed_at(previous.get("at"))
    if isinstance(before_seconds, bool) or not isinstance(before_seconds, (int, float)) or before_at is None:
        return None
    gap = (at - before_at).total_seconds()
    delta = seconds - before_seconds
    if not CPU_MIN_GAP_SEC <= gap <= CPU_MAX_GAP_SEC or delta < 0:
        return None
    return round(delta / gap / masters * 100, 1)


def previous_cpu_counters(ctx: RunContext, ids: list[str]) -> dict[str, dict]:
    """上一次保存在资源记录里的累计 CPU 秒数与采样时刻。"""
    return previous_attrs(ctx, "dcs_instance", ids, "dcs_cpu_counter")


class DcsCollector:
    """DCS V2 原生只读统计；CPU 累计秒，禁止跨版本或云监控自动回退。"""

    code, service, name = "dcs_status", "dcs", "DCS 实例状态与内存"

    def collect(self, ctx: RunContext) -> list[Observation]:  # noqa: C901  # T10
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
        scope = ctx.scope("dcs_instance")          # 采集范围：范围外的实例不参与统计与评估；全部排除时不再请求统计接口
        if not scope.open:
            insts = [i for i in insts if scope.keep({"id": i["instance_id"], "name": i["name"]})]
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
        # 逻辑节点：整个项目一次批量查询（§4.3.3），得到分片、主从、Proxy 与各节点状态；失败只影响节点判定
        nodes_by_id: dict[str, list[dict]] | None = None
        nodes_at = None
        nodes_failed = False
        if any(i.get("status") in NODE_QUERY_STATES for i in insts):
            try:
                nodes_by_id = logical_nodes(ctx)
                nodes_at = utcnow()
            except HcsError as exc:
                nodes_failed = True
                ctx.errors.append(f"DCS 节点状态：{exc}")
                log_collection_failure(log, ctx, exc, cloud_service="dcs", phase="logical_nodes")
        previous_topology = (previous_attrs(ctx, "dcs_instance", [i["instance_id"] for i in insts], "dcs_topology")
                             if nodes_failed else {})
        policy = ctx.params.get("_collection_policy") or {}
        stale_after = policy.get("stale_after_sec", 900) if isinstance(policy, dict) else 900
        if isinstance(stale_after, bool) or not isinstance(stale_after, int) or stale_after < 60:
            stale_after = 900
        ctx.stats.update(metrics_source="DCS", native_statistics=len(statistics), statistics_api_version="v2")
        if nodes_by_id is not None:
            ctx.stats["logical_nodes"] = sum(len(nodes) for nodes in nodes_by_id.values())
        counters = previous_cpu_counters(ctx, [i["instance_id"] for i in insts]) if statistics else {}
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
            topology, nodes, node_facts = instance_nodes(i, status if known_status else None, nodes_by_id, nodes_at,
                                                         nodes_failed, previous_topology.get(iid))
            attrs["dcs_topology"] = topology
            if nodes is not None:           # None：本轮接口失败，保留上次的节点清单（汇总里标注为 STALE）
                attrs["nodes"] = nodes
            seconds = metrics["used_cpu_seconds"]
            cpu = cpu_rate(counters.get(iid), seconds, at, cpu_masters(i, topology)) if native is not None else None
            if native is not None and seconds is not None and at is not None:
                attrs["dcs_cpu_counter"] = {"seconds": seconds, "at": at.isoformat()}   # 下一轮折算 CPU 的基线
            message = warning if native is None else None
            if native is None and status == "RUNNING" and not message:
                message = "统计接口未返回此实例，仅保留本次实例列表中的内存字段"
            attrs.update(metrics_source="DCS", dcs_monitoring={
                "source": "DCS", "api_version": "v2", "endpoint": "instances/statistic" if native is not None else "instances",
                "observed_at": at.isoformat() if at else None, "time_basis": "RESPONSE_RECEIVED",
                "stale_after_sec": stale_after, "warning": message,
            })
            obs.append(Observation("dcs_instance", iid, name, "instance_status", {"status": status if known_status else None},
                status_raw=status, attrs=attrs, metrics={**metrics, "cpu_usage": cpu, "connected_clients": None},
                metric_observed_at=at))
            # 无值也送入规则引擎：中断连续命中，但不恢复已有告警。
            memory = {key: metrics[key] for key in ("memory_usage", "used_memory_mb", "max_memory_mb")}
            obs.append(Observation("dcs_instance", iid, name, "memory", memory, metrics=memory, metric_observed_at=at))
            if cpu is not None:     # 能折算才有 cpu 观测；折算不了（首次采样、实例重启、间隔过长）按缺采处理，不补零
                obs.append(Observation("dcs_instance", iid, name, "cpu", {"cpu_usage": cpu},
                                       metrics={"cpu_usage": cpu}, metric_observed_at=at))
            if node_facts is not None:  # 拿到节点（或确定不适用）才有 nodes 观测；接口失败、源端未返回按缺采处理
                obs.append(Observation("dcs_instance", iid, name, "nodes", node_facts, metric_observed_at=nodes_at))
        return obs
