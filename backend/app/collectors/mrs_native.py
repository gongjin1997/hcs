"""MRS 原生作业执行兼容适配。

本局点 2026-11-05/06 已有成功调用证据；上传的 HCS 8.5.1 PDF 未列出 V2
job-executions，不能声称该 PDF 已完全确认接口或 elapsed_time 单位。只发 GET。
"""
import logging
import math
import time
from urllib.parse import quote

from app.hcs import HcsError
from app.hcs.pager import page_all, response_total

from .base import Observation, RunContext, log_collection_failure
from .scope import subject_of

log = logging.getLogger(__name__)
CLUSTER_STATES = frozenset(("starting", "running", "terminated", "failed", "abnormal", "terminating",
                            "frozen", "scaling-out", "scaling-in"))


def _clusters(ctx: RunContext) -> list[dict]:
    expected_total = None

    def fetch(params: dict) -> dict:
        data = ctx.client.get("mrs", "/v1.1/{project_id}/cluster_infos", params=params)
        nonlocal expected_total
        if not isinstance(data, dict):
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "MRS 集群响应不是对象")
        total = response_total(data, "clusterTotal", required=True)
        if expected_total is not None and total != expected_total:
            raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "MRS 集群分页总数变化，未使用不完整清单")
        expected_total = total
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



# 原生执行状态与执行结果是两个独立字段；不把 SUCCEEDED 当作执行状态。
JOB_STATES = frozenset(("NEW", "NEW_SAVING", "SUBMITTED", "ACCEPTED", "RUNNING", "FINISHED", "FAILED", "KILLED"))
JOB_RESULTS = frozenset(("UNDEFINED", "SUCCEEDED", "FAILED", "KILLED"))
TERMINAL_RESULTS = JOB_RESULTS - {"UNDEFINED"}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _millis(value: object) -> int | None:
    number = _number(value)
    # 只接受有效毫秒 epoch；秒级、负值、哨兵 0、非有限值均不猜单位。
    if number is None or not number.is_integer() or not 100_000_000_000 <= number < 253_402_300_800_000:
        return None
    return int(number)


def native_state_known(facts: dict) -> bool:
    state, result = facts.get("job_state"), facts.get("job_result")
    return (isinstance(state, str) and state in JOB_STATES
            and isinstance(result, str) and result in JOB_RESULTS
            and (state != "FINISHED" or result in TERMINAL_RESULTS))


class MrsJobCollector:
    """每个 cluster_id/job_id 保存一次执行，不按同名作业合并；无 DataArts 依赖。"""

    code, service, name = "mrs_job_status", "mrs", "MRS 原生作业执行"

    def collect(self, ctx: RunContext) -> list[Observation]:
        # 默认读取有界完整清单；提交时间过滤会漏掉早已提交、仍在运行的 Flink。
        # window_hours 属于拆分前的 DataArts 节点任务，不能隐式变成 native 的过滤授权。
        hours = ctx.params.get("submitted_window_hours")
        if hours is not None:
            hours = _number(hours)
            if hours is None or not 0 < hours <= 31 * 24:
                raise HcsError(0, "LOCAL.INVALID_CONFIG", "MRS 显式提交窗口须大于零且不超过 31 天")
        mode = ctx.params.get("job_offset_mode", "page")
        unit = ctx.params.get("elapsed_time_unit")
        if mode not in ("page", "offset") or unit not in (None, "milliseconds", "seconds"):
            raise HcsError(0, "LOCAL.INVALID_CONFIG", "MRS 分页模式或 elapsed_time_unit 配置无效")
        limits = {}
        for key, default, ceiling in (("page_size", 100, 1000), ("max_pages", 200, 200),
                                      ("max_items", 20000, 20000), ("max_clusters", 1000, 20000)):
            value = ctx.params.get(key, default)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise HcsError(0, "LOCAL.INVALID_CONFIG", f"MRS {key} 须为 1..{ceiling} 的整数")
            limits[key] = value
        scope = ctx.scope("mrs_job")
        if scope.dims - {"cluster", "job", "id"}:
            raise HcsError(0, "LOCAL.INVALID_CONFIG", "旧 workspace/node 范围不适用于 MRS 原生执行，请先迁移并重新授权范围")
        now_ms = int(time.time() * 1000)
        submitted_filter = {"submitted_time_begin": int(now_ms - hours * 3_600_000)} if hours is not None else {}
        clusters = [c for c in _clusters(ctx) if scope.keep({"cluster": (c["clusterId"], c["clusterName"])})]
        if len(clusters) > limits["max_clusters"]:
            raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "MRS 集群数量超过本轮预算，未查询作业")
        obs = []
        stats = {"source": "mrs_job_execution", "clusters": len(clusters), "executions": 0,
                 "duration_unknown": 0, "elapsed_unit_unverified": 0, "elapsed_value_invalid": 0,
                 "contract": "site_observed_hcs851_compatibility",
                 "elapsed_time_unit": unit, "job_offset_mode": mode,
                 "submitted_window_hours": hours, "legacy_window_hours_ignored": "window_hours" in ctx.params}
        ctx.stats["mrs_jobs"] = stats
        remaining = limits["max_items"]
        for cluster in clusters:
            if remaining == 0:
                ctx.errors.append("MRS 本轮执行条数达到 max_items 预算，未查询剩余集群")
                break
            cid = cluster["clusterId"]
            expected_total = None

            def fetch(params: dict, cid: str = cid) -> dict:
                nonlocal expected_total
                data = ctx.client.get("mrs", f"/v2/{{project_id}}/clusters/{quote(cid, safe='')}/job-executions",
                                      params={**params, "offset": params["offset"] + (1 if mode == "offset" else 0),
                                              **submitted_filter})
                if not isinstance(data, dict):
                    raise HcsError(0, "LOCAL.INVALID_RESPONSE", "MRS 作业响应不是对象")
                total = response_total(data, "total_record", required=True)
                if expected_total is not None and total != expected_total:
                    raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "MRS 分页总数发生变化，未使用不完整清单")
                expected_total = total
                log.info("job_collection_page", extra={"event": "job_collection_page", "cloud_service": "mrs",
                    "phase": "native_executions", "tenant_id": ctx.tenant_id, "project_ref": ctx.project_ref,
                    "cluster_id": cid, "page": params["offset"], "page_size": params["limit"],
                    "offset_mode": mode, "response_total": total,
                    "returned_count": len(data["job_list"]) if isinstance(data.get("job_list"), list) else None})
                return data

            try:
                jobs = list(page_all(fetch, "job_list", mode=mode, start=0 if mode == "offset" else 1,
                                     total_key="total_record", limit=limits["page_size"],
                                     max_pages=limits["max_pages"], max_items=remaining))
                ids = set()
                for job in jobs:
                    jid, name = job.get("job_id"), job.get("job_name")
                    if (not isinstance(jid, str) or not jid.strip() or not isinstance(name, str) or not name.strip()
                            or jid in ids or any(job.get(k) is not None and not isinstance(job[k], str)
                                                 for k in ("job_state", "job_result"))):
                        raise HcsError(0, "LOCAL.INVALID_RESPONSE", "MRS 执行 ID/名称/状态无效或分页包含重复执行")
                    ids.add(jid)
            except HcsError as exc:
                ctx.errors.append(f"cluster={cid}: {exc}")
                log_collection_failure(log, ctx, exc, cloud_service="mrs", phase="job_executions", cluster_id=cid)
                continue
            remaining -= len(jobs)
            for job in jobs:
                # 编码路径段，避免名称/ID 自带斜杠导致不同执行组合发生碰撞。
                rid = f"{quote(cid, safe='')}/{quote(job['job_id'], safe='')}"
                attrs = {"job_name": job["job_name"], "job_id": job["job_id"], "cluster_id": cid,
                         "cluster": cluster["clusterName"], "job_type": job.get("job_type"),
                         "engine": job.get("engine"), "job_state": job.get("job_state"),
                         "queue": job.get("queue") if isinstance(job.get("queue"), str) else None,
                         "job_result": job.get("job_result"), "submitted_time": job.get("submitted_time"),
                         "start_time": job.get("started_time", job.get("start_time")),
                         "end_time": job.get("finished_time", job.get("end_time")),
                         "source": "mrs_job_execution", "elapsed_time": job.get("elapsed_time"),
                         "elapsed_time_unit": unit}
                if scope.check(subject_of("mrs_job", rid, job["job_name"], attrs)) is not True:
                    continue
                facts = {"job_state": attrs["job_state"], "job_result": attrs["job_result"]}
                if not native_state_known(facts):
                    ctx.errors.append(f"cluster={cid} job_id={job['job_id']}: MRS 原生状态或结果未知，未按正常判定")
                start, end = _millis(attrs["start_time"]), _millis(attrs["end_time"])
                minutes, basis = None, None
                if start is not None and end is not None and end >= start:
                    minutes, basis = round((end - start) / 60000, 1), "start_end_milliseconds"
                elif (attrs["job_state"] == "RUNNING" and start is not None and start <= now_ms
                      and not isinstance(attrs["end_time"], bool) and attrs["end_time"] in (None, 0, "0", "")):
                    minutes, basis = round((now_ms - start) / 60000, 1), "start_now_milliseconds"
                raw = _number(attrs["elapsed_time"])
                if raw is not None and unit is None:
                    stats["elapsed_unit_unverified"] += 1
                if attrs["elapsed_time"] is not None and raw is None:
                    stats["elapsed_value_invalid"] += 1
                if minutes is None and raw is not None and unit is not None:
                    minutes, basis = round(raw / (60000 if unit == "milliseconds" else 60), 1), f"elapsed_time_{unit}"
                attrs["duration_basis"] = basis
                attrs["duration_observed_at"] = now_ms if basis == "start_now_milliseconds" else None
                if minutes is not None:
                    facts["run_minutes"] = minutes
                    facts["elapsed_min"] = minutes  # 旧人工时长规则兼容；新指标以 run_minutes 为准。
                else:
                    stats["duration_unknown"] += 1
                obs.append(Observation("mrs_job", rid, job["job_name"], "job_status", facts,
                                       status_raw=attrs["job_state"] or "UNKNOWN", attrs=attrs,
                                       metrics={"run_minutes": minutes, "elapsed_min": minutes}))
        stats["executions"] = len(obs)
        log.info("job_collection_summary", extra={"event": "job_collection_summary", "cloud_service": "mrs",
            "tenant_id": ctx.tenant_id, "project_ref": ctx.project_ref, **stats})
        ctx.stats["jobs_duration_unit_unverified"] = stats["elapsed_unit_unverified"]
        return obs
