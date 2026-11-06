"""MRS 巡检：集群状态（MRS API §6.1.3）与 MRS 作业执行（经 DataArts 作业实例详情的 MRS 类节点）。

HCS 8.5.1 的 MRS API 参考只提供集群类查询，作业通过 DataArts Studio 编排：数据治理中心 API
§5.3.16「查询作业实例列表」+ §5.3.17「查询作业实例详情」返回各节点的 type / status / 起止时间，
type 为 MRSSpark、MapReduce、MRSFlinkJob、MRSHetuEngine 的节点即 MRS 作业。
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any
from urllib.parse import quote

from sqlalchemy import select

from app.hcs import HcsError
from app.hcs.pager import page_all

from .base import Observation, RunContext, log_collection_failure
from .dataarts import BATCH_STATES, dataarts_error, millis, paged_list, text_or_none
from .scope import subject_of

log = logging.getLogger(__name__)
CLUSTER_STATES = frozenset(("starting", "running", "terminated", "failed", "abnormal", "terminating",
                            "frozen", "scaling-out", "scaling-in"))
# 数据治理中心 API 表 5-182 Node.type 中执行 MRS 服务作业的节点类型
MRS_NODE_TYPES = frozenset(("MRSSpark", "MapReduce", "MRSFlinkJob", "MRSHetuEngine"))
# 表 5-182 Node.status
NODE_STATES = frozenset(("waiting", "running", "success", "fail", "skip", "pause", "manual-stop"))
# 实例进入终态后详情不再变化：本地已有记录时直接复用，不重复查询
TERMINAL_INSTANCE_STATES = frozenset(("success", "fail", "manual-stop"))
MAX_WINDOW_HOURS = 72          # §5.3.16：minPlanTime 与 maxPlanTime 的查询范围不超过 3 天
DEFAULT_MAX_DETAILS = 500       # 每个工作空间每轮最多查询的实例详情数（超出记为局部失败，不静默截断）


def _clusters(ctx: RunContext) -> list[dict]:
    def fetch(params: dict) -> dict:
        data = ctx.client.get("mrs", "/v1.1/{project_id}/cluster_infos", params=params)
        if "clusterTotal" not in data or data["clusterTotal"] is None:
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "MRS 集群总数缺失，未使用不完整清单")
        return data

    clusters = list(page_all(fetch, "clusters",
                             mode="page", start=1, total_key="clusterTotal", offset_param="currentPage",
                             limit_param="pageSize", limit=50))
    ids: set[str] = set()
    for cluster in clusters:
        cid, name = cluster.get("clusterId"), cluster.get("clusterName")
        if not isinstance(cid, str) or not cid or not isinstance(name, str) or not name or cid in ids:
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "MRS 集群标识缺失或重复，未使用不完整清单")
        if cluster.get("clusterState") is not None and not isinstance(cluster["clusterState"], str):
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "MRS 集群状态字段格式不正确")
        ids.add(cid)
    return clusters


class MrsClusterCollector:
    code, service, name = "mrs_cluster_status", "mrs", "MRS 集群状态"

    def collect(self, ctx: RunContext) -> list[Observation]:
        observations = []
        scope = ctx.scope("mrs_cluster")
        for cluster in _clusters(ctx):
            if not scope.keep({"id": cluster["clusterId"], "name": cluster["clusterName"]}):
                continue                        # 采集范围：范围外的集群不评估
            state = cluster.get("clusterState")
            known = state in CLUSTER_STATES
            if not known:
                ctx.errors.append(f"cluster={cluster['clusterId']}: MRS 集群状态缺失或未被上传 PDF 确认，未按正常状态判定")
            observations.append(Observation(
                "mrs_cluster", cluster["clusterId"], cluster["clusterName"], "cluster_status",
                {"cluster_state": state} if known else {}, status_raw=state or "UNKNOWN",
                attrs={"version": cluster.get("clusterVersion"), "nodes": cluster.get("totalNodeNum")}))
        return observations


def _node_minutes(node: dict, now_ms: int) -> float | None:
    start, end = millis(node.get("startTime")), millis(node.get("endTime"))
    if start is None or start == 0:
        return None
    if end is None or end == 0:
        end = now_ms if node.get("status") == "running" else None
    return round((end - start) / 60000, 1) if end is not None and end >= start else None


def _detail_nodes(body: dict, job_name: str, instance_id: int) -> list[dict]:
    """校验 §5.3.17 响应：实例必须与请求一致，节点名称唯一，type / status 为字符串。"""
    if not isinstance(body, dict) or body.get("error_code"):
        raise dataarts_error("DataArts 未返回有效作业实例详情")
    if body.get("jobName") is not None and body["jobName"] != job_name:
        raise dataarts_error("DataArts 实例详情的作业名与请求不一致")
    if body.get("instanceId") is not None and millis(body["instanceId"]) != instance_id:
        raise dataarts_error("DataArts 实例详情的实例 ID 与请求不一致")
    nodes = body.get("nodes")
    if not isinstance(nodes, list) or any(
            not isinstance(n, dict) or not isinstance(n.get("nodeName"), str) or not n["nodeName"]
            or (n.get("type") is not None and not isinstance(n["type"], str))
            or (n.get("status") is not None and not isinstance(n["status"], str)) for n in nodes):
        raise dataarts_error("DataArts 实例详情的节点列表、名称、类型或状态格式不正确")
    if len({n["nodeName"] for n in nodes}) != len(nodes):
        raise dataarts_error("DataArts 实例详情包含重复节点名")
    return nodes


class MrsJobCollector:
    """MRS 作业执行：DataArts 实例列表（§5.3.16）→ 每个作业最新实例的详情（§5.3.17）→ MRS 类节点。

    资源 ID 为 [工作空间/]作业名/节点名；执行标识为实例 ID（实例变化时状态持续时间重新计时）。
    参数：window_hours（默认 24，≤72）、max_details（每个工作空间每轮详情上限，默认 500）、hcs_concurrency。

    减少重复查询：实例进入 success / fail / manual-stop 后详情不再变化。含 MRS 节点的终态实例复用本地资源记录；
    不含 MRS 节点的终态实例把 (工作空间/作业名 → 实例 ID) 记入 sys_setting，下一轮不再查询其详情。
    """

    code, service, name = "mrs_job_status", "mrs", "MRS 作业执行"

    def collect(self, ctx: RunContext) -> list[Observation]:
        hours = ctx.params.get("window_hours", 24)
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= MAX_WINDOW_HOURS:
            raise HcsError(0, "LOCAL.INVALID_CONFIG", f"MRS 作业查询窗口须大于零且不超过 {MAX_WINDOW_HOURS} 小时")
        limit = ctx.params.get("max_details", DEFAULT_MAX_DETAILS)
        if type(limit) is not int or limit < 1:
            raise HcsError(0, "LOCAL.INVALID_CONFIG", "max_details 须为正整数")
        now_ms = int(time.time() * 1000)
        window = {"minPlanTime": int(now_ms - hours * 3600 * 1000), "maxPlanTime": now_ms}
        known = self._terminal_snapshots(ctx)
        scanned = self._scan_state(ctx)
        seen_keys: dict[str, int | None] = {}
        obs: list[Observation] = []
        stats: dict[str, Any] = {
            "source": "dataarts_instance_detail", "instances": 0, "details_queried": 0, "details_reused": 0,
            "details_skipped": 0, "jobs_without_mrs_nodes": 0, "mrs_nodes": 0}
        scope = ctx.scope("mrs_job")
        for ws in dict.fromkeys(ctx.workspaces or [""]):
            if not scope.keep({"workspace": ws}):      # 采集范围：整个工作空间在范围外，不请求实例列表
                continue
            headers = {"workspace": ws} if ws else None

            def fetch_instances(p: dict, h: dict | None = headers) -> dict:
                return ctx.client.get("dataarts", "/v1/{project_id}/jobs/instances/detail",
                                      params={**window, **p}, headers=h)

            try:
                insts = paged_list(fetch_instances, "instances")
                if any(not isinstance(i.get("jobName"), str) or not i["jobName"]
                       or millis(i.get("instanceId")) is None or not isinstance(i.get("status"), str) for i in insts):
                    raise dataarts_error("DataArts 实例清单的 jobName、instanceId 或 status 不完整")
            except HcsError as e:
                ctx.errors.append(f"workspace={ws}: {e}")
                log_collection_failure(log, ctx, e, cloud_service="dataarts", phase="mrs_job_instances", workspace=ws)
                continue
            latest: dict[str, dict] = {}

            def order(i: dict) -> tuple[int, int, int]:
                return (millis(i.get("planTime")) or 0, millis(i.get("submitTime")) or 0,
                        millis(i.get("instanceId")) or 0)
            for i in insts:
                if not scope.keep({"workspace": ws, "job": i["jobName"]}):    # 范围外的作业不查询实例详情
                    continue
                if i["jobName"] not in latest or order(i) > order(latest[i["jobName"]]):
                    latest[i["jobName"]] = i
            stats["instances"] += len(latest)
            pending: list[dict] = []
            for name, inst in latest.items():
                iid, terminal = millis(inst["instanceId"]), inst["status"] in TERMINAL_INSTANCE_STATES
                key = f"{ws}/{name}"
                reused = known.get((ws, name, iid))
                if terminal and reused:
                    obs.extend(reused)
                    stats["details_reused"] += 1
                elif terminal and scanned.get(key) == iid:
                    seen_keys[key] = iid
                    stats["details_skipped"] += 1
                else:
                    pending.append(inst)
            if len(pending) > limit:
                ctx.errors.append(f"workspace={ws}: 待查询实例详情 {len(pending)} 个，超过上限 {limit}，"
                                  "本轮仅查询计划时间最新的部分；可调大 max_details 或缩短 window_hours")
                pending = sorted(pending, key=order, reverse=True)[:limit]

            def fetch_detail(inst: dict, h: dict | None = headers) -> tuple[dict, list[dict] | None, HcsError | None]:
                iid = millis(inst["instanceId"])
                assert iid is not None
                try:
                    body = ctx.client.get("dataarts", f"/v1/{{project_id}}/jobs/{quote(inst['jobName'], safe='')}"
                                          f"/instances/{iid}", headers=h)
                    return inst, _detail_nodes(body, inst["jobName"], iid), None
                except HcsError as e:
                    return inst, None, e

            concurrency = max(1, min(16, int(ctx.params.get("hcs_concurrency", 4))))
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="dataarts-mrs") as pool:
                results = [f.result() for f in [pool.submit(copy_context().run, fetch_detail, i) for i in pending]]
            stats["details_queried"] += len(results)
            failures = 0
            for inst, nodes, error in results:
                if error is not None or nodes is None:
                    failures += 1
                    ctx.errors.append(f"job={inst['jobName']}: {error}")
                    if error is not None and failures <= 5:
                        log_collection_failure(log, ctx, error, cloud_service="dataarts", phase="mrs_job_detail",
                                               workspace=ws, resource_name=inst["jobName"])
                    continue
                found = [n for n in nodes if n.get("type") in MRS_NODE_TYPES]
                if not found:
                    stats["jobs_without_mrs_nodes"] += 1
                    if inst["status"] in TERMINAL_INSTANCE_STATES:
                        seen_keys[f"{ws}/{inst['jobName']}"] = millis(inst["instanceId"])
                obs.extend(self._observation(ws, inst, n, now_ms, ctx) for n in found)
        if not scope.open:     # 节点级规则：详情与复用的快照里都可能含范围外的节点（"无 MRS 节点"的判断不受影响）
            obs = [o for o in obs if scope.keep(subject_of(o.resource_type, o.resource_id, o.name, o.attrs))]
        stats["mrs_nodes"] = len(obs)
        ctx.stats.update(mrs_jobs=stats)
        if not ctx.errors:  # 有局部失败时本轮清单不完整，保留旧记录，避免丢掉未扫描作业的结论
            self._save_scan_state(ctx, seen_keys)
        return obs

    @staticmethod
    def _observation(ws: str, inst: dict, node: dict, now_ms: int, ctx: RunContext) -> Observation:
        job, node_name, status = inst["jobName"], node["nodeName"], node.get("status")
        if status not in NODE_STATES:
            ctx.errors.append(f"job={job} node={node_name}: 未识别的 DataArts 节点状态，未按正常状态判定")
        facts: dict = {"node_status": status} if status in NODE_STATES else {}
        if inst["status"] in BATCH_STATES:
            facts["job_status"] = inst["status"]
        minutes = _node_minutes(node, now_ms)
        if minutes is not None:
            facts["run_minutes"] = minutes
        retries = node.get("retryTimes")
        retries = retries if type(retries) is int and retries >= 0 else None
        rid = f"{ws}/{job}/{node_name}" if ws else f"{job}/{node_name}"
        return Observation(
            "mrs_job", rid, f"{job} / {node_name}", "job_status", facts, status_raw=status or "UNKNOWN",
            attrs={"workspace": ws, "job_name": job, "node_name": node_name, "node_type": node.get("type"),
                   "instance_id": millis(inst["instanceId"]), "job_status": inst["status"],
                   "node_status": status, "instance_type": inst.get("instanceType"),
                   "plan_time": millis(inst.get("planTime")), "start_time": millis(node.get("startTime")) or None,
                   "end_time": millis(node.get("endTime")) or None, "retry_times": retries,
                   "log_path": text_or_none(node.get("logPath")), "source": "dataarts_instance_detail"},
            metrics={"run_minutes": minutes, "retry_times": retries})

    @staticmethod
    def _scan_key(ctx: RunContext) -> str:
        return f"mrs_job_scan:{ctx.tenant_id}:{ctx.project_ref}"

    @classmethod
    def _scan_state(cls, ctx: RunContext) -> dict[str, int]:
        if ctx.db is None:
            return {}
        from app.models import SysSetting

        row = ctx.db.get(SysSetting, cls._scan_key(ctx))
        value = row.value.get("instances") if row is not None and isinstance(row.value, dict) else None
        return {k: v for k, v in value.items() if isinstance(k, str) and type(v) is int} if isinstance(value, dict) else {}

    @classmethod
    def _save_scan_state(cls, ctx: RunContext, scanned: dict[str, int | None]) -> None:
        if ctx.db is None:
            return
        state = {k: v for k, v in scanned.items() if v is not None}
        from app.models import SysSetting

        key = cls._scan_key(ctx)
        row = ctx.db.get(SysSetting, key)
        if row is None:
            ctx.db.add(SysSetting(key=key, value={"instances": state}, updated_by="system:mrs-job-scan"))
        elif (row.value or {}).get("instances") != state:
            row.value = {"instances": state}

    @staticmethod
    def _terminal_snapshots(ctx: RunContext) -> dict[tuple[str, str, int | None], list[Observation]]:
        """本地已记录且实例已终态的 MRS 节点：同一实例再次出现时复用，避免对不变的详情重复查询。"""
        if ctx.db is None:
            return {}
        from app.models import Resource

        out: dict[tuple[str, str, int | None], list[Observation]] = {}
        for res in ctx.db.scalars(select(Resource).where(
                Resource.tenant_id == ctx.tenant_id, Resource.project_ref == ctx.project_ref,
                Resource.resource_type == "mrs_job", Resource.lifecycle == "ACTIVE")):
            a = res.attrs or {}
            if (a.get("source") != "dataarts_instance_detail" or a.get("job_status") not in TERMINAL_INSTANCE_STATES
                    or not isinstance(a.get("job_name"), str) or not isinstance(a.get("node_name"), str)):
                continue
            facts: dict = {"job_status": a["job_status"]}
            if a.get("node_status") in NODE_STATES:
                facts["node_status"] = a["node_status"]
            minutes = (res.metrics or {}).get("run_minutes")
            if isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
                facts["run_minutes"] = minutes
            keep = {k: a.get(k) for k in ("workspace", "job_name", "node_name", "node_type", "instance_id", "job_status",
                                          "node_status", "instance_type", "plan_time", "start_time", "end_time",
                                          "retry_times", "log_path", "source")}
            out.setdefault((a.get("workspace") or "", a["job_name"], millis(a.get("instance_id"))), []).append(
                Observation("mrs_job", res.resource_id, res.name, "job_status", facts,
                            status_raw=a.get("node_status") or "UNKNOWN", attrs=keep,
                            metrics={"run_minutes": facts.get("run_minutes"), "retry_times": a.get("retry_times")}))
        return out
