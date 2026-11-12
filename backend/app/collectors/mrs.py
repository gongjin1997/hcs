"""DataArts 实例详情中的 MRS 节点，与 MRS 原生执行独立采集。"""
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from hashlib import sha256
from typing import Any
from urllib.parse import quote

from sqlalchemy import select

from app.hcs import HcsError

from .base import Observation, RunContext, log_collection_failure
from .dataarts import BATCH_STATES, dataarts_error, millis, paged_list, text_or_none
from .mrs_native import CLUSTER_STATES, MrsClusterCollector, MrsJobCollector  # noqa: F401 兼容原导入路径
from .mrs_params import mrs_progress
from .scope import subject_of

log = logging.getLogger(__name__)
# 数据治理中心 API 表 5-182；HiveSQL/SparkSQL 是 MRS Hive/Spark SQL，不造额外枚举。
MRS_NODE_TYPES = frozenset(("MRSSpark", "MapReduce", "MRSFlinkJob", "MRSHetuEngine", "HiveSQL", "SparkSQL"))
NODE_STATES = frozenset(("waiting", "running", "success", "fail", "skip", "pause", "manual-stop"))
TERMINAL_INSTANCE_STATES = frozenset(("success", "fail", "manual-stop"))
MAX_WINDOW_HOURS = 72
DEFAULT_MAX_DETAILS = 500
NODE_CACHE_VERSION = 2  # 扩大合法节点类型后，旧负缓存与完整性摘要一并失效。


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


def _node_manifest(names: list[str]) -> dict:
    """过滤前完整 MRS 节点清单的摘要；每个资源只保存固定大小的完整性证据。"""
    payload = json.dumps(sorted(names), ensure_ascii=False).encode("utf-8")
    return {"version": NODE_CACHE_VERSION, "count": len(names), "digest": sha256(payload).hexdigest()}


class DataArtsMrsNodeCollector:
    """DataArts MRS 节点：DataArts 实例列表（§5.3.16）→ 每个作业最新实例的详情（§5.3.17）→ MRS 类节点。

    资源 ID 为 [工作空间/]作业名/节点名；执行标识为实例 ID（实例变化时状态持续时间重新计时）。
    参数：window_hours（默认 24，≤72）、max_details（每个工作空间每轮详情上限，默认 500）、hcs_concurrency。

    减少重复查询：实例进入 success / fail / manual-stop 后详情不再变化。含 MRS 节点的终态实例仅在本地记录完整时复用；
    不含 MRS 节点的终态实例把 (工作空间/作业名 → 实例 ID) 记入 sys_setting，下一轮不再查询其详情。
    """

    code, service, name = "dgc_mrs_node_status", "dataarts", "DataArts MRS 节点状态"

    def collect(self, ctx: RunContext) -> list[Observation]:  # noqa: C901  # T10
        scope = ctx.scope("dgc_mrs_node")
        if scope.dims - {"workspace", "job", "node"}:
            raise HcsError(0, "LOCAL.INVALID_CONFIG", "旧 native 范围不能映射为 DataArts 节点授权，请重新确认范围")
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
        for ws in dict.fromkeys(ctx.workspaces or [""]):
            if not scope.keep({"workspace": ws}):      # 采集范围：整个工作空间在范围外，不请求实例列表
                continue
            headers = {"workspace": ws} if ws else None
            mrs_progress(ctx, "dataarts_mrs_workspace", workspace=ws, message="读取DataArts实例")

            def fetch_instances(p: dict, h: dict | None = headers, workspace: str = ws) -> dict:
                mrs_progress(ctx, "dataarts_mrs_instances_page", unit="page", workspace=workspace, page=p["offset"],
                             message="读取实例分页")
                body = ctx.client.get("dataarts", "/v1/{project_id}/jobs/instances/detail",
                                      params={**window, **p}, headers=h)
                mrs_progress(ctx, "dataarts_mrs_instances_page", current=p["offset"] + 1, unit="page", workspace=workspace,
                             response_total=body.get("total") if isinstance(body, dict) else None,
                             returned_count=len(body["instances"]) if isinstance(body, dict)
                             and isinstance(body.get("instances"), list) else None)
                return body

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
            results: list[tuple[dict, list[dict] | None, HcsError | None]] = []
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="dataarts-mrs") as pool:
                # 复用原详情线程池；只提交一批，批间由主线程进度回调检查执行器deadline/租约。
                for start in range(0, len(pending), concurrency):
                    mrs_progress(ctx, "dataarts_mrs_details", current=len(results), total=len(pending), unit="instance",
                                 workspace=ws, message="读取实例详情批次")
                    futures = [pool.submit(copy_context().run, fetch_detail, i) for i in pending[start:start + concurrency]]
                    results.extend(f.result() for f in futures)
                    mrs_progress(ctx, "dataarts_mrs_details", current=len(results), total=len(pending), unit="instance",
                                 workspace=ws)
            stats["details_queried"] += len(results)
            failures = 0
            for inst, nodes, error in results:
                mrs_progress(ctx, "dataarts_mrs_nodes_process", current=len(obs), unit="node", workspace=ws,
                             instance_id=millis(inst["instanceId"]))
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
                manifest = _node_manifest([n["nodeName"] for n in found])
                for n in found:
                    observation = self._observation(ws, inst, n, now_ms, ctx)
                    observation.attrs["mrs_node_manifest"] = manifest
                    obs.append(observation)
                stats["mrs_nodes"] = len(obs)
        if not scope.open:     # 节点级规则：详情与复用的快照里都可能含范围外的节点（"无 MRS 节点"的判断不受影响）
            obs = [o for o in obs if scope.keep(subject_of(o.resource_type, o.resource_id, o.name, o.attrs))]
        stats["mrs_nodes"] = len(obs)
        ctx.stats.update(dgc_mrs_nodes=stats)
        mrs_progress(ctx, "dataarts_mrs_complete", current=len(obs), unit="node", instances=stats["instances"],
                     details_queried=stats["details_queried"], details_reused=stats["details_reused"])
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
            "dgc_mrs_node", rid, f"{job} / {node_name}", "job_status", facts, status_raw=status or "UNKNOWN",
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
        value = row.value.get("instances") if (row is not None and isinstance(row.value, dict)
                                               and row.value.get("version") == NODE_CACHE_VERSION) else None
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
            ctx.db.add(SysSetting(key=key, value={"version": NODE_CACHE_VERSION, "instances": state},
                                  updated_by="system:mrs-job-scan"))
        elif (row.value or {}).get("version") != NODE_CACHE_VERSION or (row.value or {}).get("instances") != state:
            row.value = {"version": NODE_CACHE_VERSION, "instances": state}

    @staticmethod
    def _terminal_snapshots(ctx: RunContext) -> dict[tuple[str, str, int | None], list[Observation]]:
        """只复用有完整性证据的终态实例；旧记录或范围过滤后的子集须重新查询详情。"""
        if ctx.db is None:
            return {}
        from app.models import Resource

        out: dict[tuple[str, str, int | None], list[Observation]] = {}
        for res in ctx.db.scalars(select(Resource).where(
                Resource.tenant_id == ctx.tenant_id, Resource.project_ref == ctx.project_ref,
                Resource.resource_type == "dgc_mrs_node", Resource.lifecycle == "ACTIVE")):
            a = res.attrs or {}
            if (a.get("source") != "dataarts_instance_detail" or a.get("job_status") not in TERMINAL_INSTANCE_STATES
                    or a.get("node_status") not in NODE_STATES
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
                                          "retry_times", "log_path", "source", "mrs_node_manifest")}
            out.setdefault((a.get("workspace") or "", a["job_name"], millis(a.get("instance_id"))), []).append(
                Observation("dgc_mrs_node", res.resource_id, res.name, "job_status", facts,
                            status_raw=a.get("node_status") or "UNKNOWN", attrs=keep,
                            metrics={"run_minutes": facts.get("run_minutes"), "retry_times": a.get("retry_times")}))
        complete: dict[tuple[str, str, int | None], list[Observation]] = {}
        for key, group in out.items():
            manifest = _node_manifest([o.attrs["node_name"] for o in group])
            if all(o.attrs.get("mrs_node_manifest") == manifest for o in group):
                complete[key] = group
        return complete
