"""MRS 原生作业执行兼容适配。

本局点 2026-11-05/06 已有成功调用证据；上传的 HCS 8.5.1 PDF 未列出 V2
job-executions，不能声称该 PDF 已完全确认接口或 elapsed_time 单位。只发 GET。
"""
import logging
import math
import time
from urllib.parse import quote

from app.hcs import HcsError
from app.hcs.pager import page_all, response_items, response_total
from app.hcs.ratelimit import request_budget

from .base import Observation, RunContext, log_collection_failure
from .mrs_params import mrs_progress, native_queries, validate_mrs_params
from .scope import subject_of

log = logging.getLogger(__name__)
CLUSTER_STATES = frozenset(("starting", "running", "terminated", "failed", "abnormal", "terminating",
                            "frozen", "scaling-out", "scaling-in"))


def _clusters(ctx: RunContext, request=None) -> list[dict]:
    expected_total = None

    def fetch(params: dict) -> dict:
        mrs_progress(ctx, "mrs_clusters", current=params["currentPage"] - 1, unit="page",
                     page=params["currentPage"], message="读取集群清单")
        started = time.perf_counter()
        try:
            data = (request("/v1.1/{project_id}/cluster_infos", params) if request is not None
                    else ctx.client.get("mrs", "/v1.1/{project_id}/cluster_infos", params=params))
        except HcsError as exc:
            log.warning("mrs_api_page_failed", extra={"event": "mrs_api_page_failed", "cloud_service": "mrs",
                        "phase": "cluster_discovery", "page": params["currentPage"],
                        "cost_ms": round((time.perf_counter() - started) * 1000, 2),
                        "error_code": exc.code, "error_type": type(exc).__name__})
            raise
        cost_ms = round((time.perf_counter() - started) * 1000, 2)
        nonlocal expected_total
        if not isinstance(data, dict):
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "MRS 集群响应不是对象")
        total = response_total(data, "clusterTotal", required=True)
        if expected_total is not None and total != expected_total:
            raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "MRS 集群分页总数变化，未使用不完整清单")
        expected_total = total
        total_pages = max(1, math.ceil(total / params["pageSize"]))
        mrs_progress(ctx, "mrs_clusters", current=params["currentPage"], total=total_pages, unit="page",
                     response_total=total,
                     returned_count=len(data["clusters"]) if isinstance(data.get("clusters"), list) else None)
        log.info("mrs_api_page_completed", extra={"event": "mrs_api_page_completed", "cloud_service": "mrs",
                 "phase": "cluster_discovery", "page": params["currentPage"], "page_size": params["pageSize"],
                 "response_total": total, "returned_count": len(data["clusters"])
                 if isinstance(data.get("clusters"), list) else None, "cost_ms": cost_ms})
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


class _BudgetExceeded(HcsError):
    def __init__(self, key: str):
        super().__init__(0, "LOCAL.MRS_BUDGET", f"MRS 整轮 {key} 预算耗尽，覆盖不可确认，未继续请求")


class _NativeBudget:
    def __init__(self, ctx: RunContext, config: dict, stats: dict):
        self.ctx, self.config, self.stats = ctx, config, stats

    def request(self, path: str, params: dict, *, jobs: bool = False) -> dict:
        if self.stats["calls"] >= self.config["max_calls"]:
            raise _BudgetExceeded("max_calls")
        if jobs:
            if self.stats["pages"] >= self.config["max_pages"]:
                raise _BudgetExceeded("max_pages")
            if self.stats["items_returned"] >= self.config["max_items"]:
                raise _BudgetExceeded("max_items")
            self.stats["pages"] += 1
        self.stats["calls"] += 1  # 失败逻辑调用计数；底层重试/鉴权尝试另受外层共享请求额度约束。
        try:
            return self.ctx.client.get("mrs", path, params=params)
        except HcsError as exc:
            if exc.code == "LOCAL.REQUEST_BUDGET":
                raise _BudgetExceeded("upstream_request_budget") from exc
            raise


def _query_jobs(ctx: RunContext, budget: _NativeBudget, cid: str, label: str, filters: dict) -> list[dict]:
    config, stats = budget.config, budget.stats
    mode = config["job_offset_mode"]
    available = config["max_items"] - stats["items_returned"]
    query: dict = {"cluster_id": cid, "query": label, "filters": filters, "pages": 0, "items": 0,
             "total": None, "coverage": "collecting"}
    stats["queries"].append(query)
    expected_total = None
    ids: set[str] = set()

    def fetch(params: dict) -> dict:
        nonlocal expected_total
        # 全轮同一个时间窗；只在明确授权的 all_history 模式发送无筛选查询。
        params = {**params, "offset": params["offset"] + (1 if mode == "offset" else 0), **filters}
        mrs_progress(ctx, "mrs_executions_page", current=query["pages"], unit="page", cluster_id=cid,
                     query=label, page=params["offset"], calls=stats["calls"],
                     items_returned=stats["items_returned"], message="读取执行分页")
        calls_before = stats["calls"]
        started = time.perf_counter()
        try:
            data = budget.request(f"/v2/{{project_id}}/clusters/{quote(cid, safe='')}/job-executions", params,
                                  jobs=True)
        except HcsError as exc:
            log.warning("mrs_api_page_failed", extra={"event": "mrs_api_page_failed", "cloud_service": "mrs",
                        "phase": "job_executions", "cluster_id": cid, "query": label, "page": params["offset"],
                        "request_attempted": stats["calls"] > calls_before,
                        "cost_ms": round((time.perf_counter() - started) * 1000, 2),
                        "error_code": exc.code, "error_type": type(exc).__name__})
            raise
        request_cost_ms = round((time.perf_counter() - started) * 1000, 2)
        query["pages"] += 1
        if not isinstance(data, dict):
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "MRS 作业响应不是对象")
        items = response_items(data, "job_list")
        stats["items_returned"] += len(items)
        query["items"] += len(items)
        if len(items) > params["limit"]:
            raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "MRS 返回超过请求页大小，分页覆盖不可确认")
        total = response_total(data, "total_record", required=True)
        query["total"] = total
        if expected_total is not None and total != expected_total:
            raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "MRS 分页总数发生变化，未使用不完整清单")
        expected_total = total
        for job in items:
            jid, name = job.get("job_id"), job.get("job_name")
            if (not isinstance(jid, str) or not jid.strip() or not isinstance(name, str) or not name.strip()
                    or jid in ids or any(job.get(k) is not None and not isinstance(job[k], str)
                                         for k in ("job_state", "job_result"))):
                raise HcsError(0, "LOCAL.INVALID_RESPONSE", "MRS 执行 ID/名称/状态无效或分页包含重复执行")
            ids.add(jid)
            if "job_state" in filters and job.get("job_state") != filters["job_state"]:
                raise HcsError(0, "LOCAL.MRS_FILTER_UNCONFIRMED", "MRS 返回状态不符合筛选，可能忽略筛选，覆盖不可确认")
            if "submitted_time_begin" in filters:
                submitted = _millis(job.get("submitted_time"))
                if submitted is None or not filters["submitted_time_begin"] <= submitted <= filters["submitted_time_end"]:
                    raise HcsError(0, "LOCAL.MRS_FILTER_UNCONFIRMED", "MRS 提交时间缺失/窗外，可能忽略筛选，覆盖不可确认")
        mrs_progress(ctx, "mrs_executions_page", current=query["pages"],
                     total=max(1, math.ceil(total / params["limit"])), unit="page", cluster_id=cid,
                     query=label, page=params["offset"], query_items=query["items"], query_total=total,
                     message=f"读取 {label} 执行分页",
                     returned_count=len(items), items_returned=stats["items_returned"], calls=stats["calls"])
        log.info("job_collection_page", extra={"event": "job_collection_page", "cloud_service": "mrs",
                 "phase": "native_executions", "tenant_id": ctx.tenant_id, "project_ref": ctx.project_ref,
                 "cluster_id": cid, "query": label, "page": params["offset"], "page_size": params["limit"],
                 "offset_mode": mode, "response_total": total, "returned_count": len(items),
                 "cost_ms": request_cost_ms, **filters})
        if stats["items_returned"] > config["max_items"] or total > available:
            raise _BudgetExceeded("max_items")
        return data

    try:
        if available <= 0:
            raise _BudgetExceeded("max_items")
        jobs = list(page_all(fetch, "job_list", mode=mode, start=0 if mode == "offset" else 1,
                             total_key="total_record", limit=min(config["page_size"], available),
                             max_pages=config["max_pages"], max_items=available))
    except HcsError:
        query["coverage"] = "unconfirmed"
        raise
    query["coverage"] = "response_validated"
    stats["queries_completed"] += 1
    return jobs


class MrsJobCollector:
    """每个 cluster_id/job_id 保存一次执行，不按同名作业合并；无 DataArts 依赖。"""

    code, service, name = "mrs_job_status", "mrs", "MRS 原生作业执行"

    def collect(self, ctx: RunContext) -> list[Observation]:
        config = validate_mrs_params(self.code, ctx.params, strict=False)
        # 共享现有请求预算，使 HcsClient 内部重试/鉴权也不能刷新整轮调用额度。
        # infinity 不设采集器自己的硬deadline；已有上层deadline由 request_budget 取更早值继承。
        with request_budget(attempts=config["max_calls"], seconds=math.inf):
            return self._collect(ctx, config)

    def _collect(self, ctx: RunContext, config: dict) -> list[Observation]:
        mode, unit = config["job_offset_mode"], config["elapsed_time_unit"]
        scope = ctx.scope("mrs_job")
        if scope.dims - {"cluster", "job", "id"}:
            raise HcsError(0, "LOCAL.INVALID_CONFIG", "旧 workspace/node 范围不适用于 MRS 原生执行，请先迁移并重新授权范围")
        now_ms = int(time.time() * 1000)
        queries = native_queries(config, now_ms)  # 时间窗错误也必须在第一条云请求前拒绝。
        obs: list[Observation] = []
        stats = {"source": "mrs_job_execution", "clusters": 0, "clusters_completed": 0, "executions": 0,
                 "duration_unknown": 0, "elapsed_unit_unverified": 0, "elapsed_value_invalid": 0,
                 "contract": "site_observed_hcs851_compatibility",
                 "elapsed_time_unit": unit, "job_offset_mode": mode,
                 "query_mode": config["query_mode"], "submitted_window_hours": config.get("submitted_window_hours"),
                 "legacy_window_hours_ignored": "window_hours" in ctx.params,
                 "calls": 0, "pages": 0, "items_returned": 0, "duplicates": 0, "queries": [],
                 "queries_completed": 0, "queries_planned": None, "coverage_complete": False,
                 "coverage": "collecting", "filter_contract": "public_sdk_not_site_verified",
                 "calls_semantics": "logical_get_attempts; wire_attempts_also_bounded_by_shared_request_budget",
                 "filter_evidence": "response_consistency_only_no_proof_of_non_omission",
                 "budgets": {k: config[k] for k in ("max_calls", "max_pages", "max_items", "max_clusters", "page_size")}}
        ctx.stats["mrs_jobs"] = stats
        budget = _NativeBudget(ctx, config, stats)
        try:
            clusters = [c for c in _clusters(ctx, budget.request)
                        if scope.keep({"cluster": (c["clusterId"], c["clusterName"])})]
            stats["clusters"] = len(clusters)
            stats["queries_planned"] = len(clusters) * len(queries)
            if len(clusters) > config["max_clusters"]:
                raise _BudgetExceeded("max_clusters")
        except HcsError as exc:
            stats["coverage"] = "unconfirmed"
            ctx.errors.append(f"MRS 集群清单未完成: {exc}")
            log_collection_failure(log, ctx, exc, cloud_service="mrs", phase="native_clusters")
            return []
        exhausted = False
        for cluster_index, cluster in enumerate(clusters):
            cid = cluster["clusterId"]
            mrs_progress(ctx, "mrs_cluster_executions", current=cluster_index, total=len(clusters), unit="cluster",
                         cluster_id=cid, executions=len(obs), message="读取范围内集群执行")
            jobs_by_id: dict[str, dict] = {}
            cluster_complete = True
            for label, filters in queries:
                try:
                    jobs = _query_jobs(ctx, budget, cid, label, filters)
                except HcsError as exc:
                    cluster_complete = False
                    ctx.errors.append(f"cluster={cid} query={label}: {exc}")
                    log_collection_failure(log, ctx, exc, cloud_service="mrs", phase="job_executions", cluster_id=cid)
                    if isinstance(exc, _BudgetExceeded):
                        exhausted = True
                        break
                    continue
                for job in jobs:
                    stats["duplicates"] += int(job["job_id"] in jobs_by_id)
                    jobs_by_id[job["job_id"]] = job  # 后查的响应更新状态；不按名称合并。
                mrs_progress(ctx, "mrs_executions_read", current=len(jobs_by_id), unit="execution", cluster_id=cid,
                             query=label, queries_completed=stats["queries_completed"], items_returned=stats["items_returned"])
            for job_index, job in enumerate(jobs_by_id.values()):
                if job_index % 100 == 0:
                    mrs_progress(ctx, "mrs_executions_process", current=job_index, total=len(jobs_by_id), unit="execution",
                                 cluster_id=cid, executions=len(obs))
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
            stats["clusters_completed"] += int(cluster_complete)
            mrs_progress(ctx, "mrs_executions_process", current=len(jobs_by_id), total=len(jobs_by_id), unit="execution",
                         cluster_id=cid, executions=len(obs), coverage_complete=cluster_complete)
            if exhausted:
                break
        stats["executions"] = len(obs)
        stats["coverage_complete"] = stats["clusters_completed"] == len(clusters) and not exhausted
        stats["coverage"] = "response_validated" if stats["coverage_complete"] else "unconfirmed"
        mrs_progress(ctx, "mrs_collection_complete", current=stats["clusters_completed"], total=len(clusters), unit="cluster",
                     executions=len(obs), calls=stats["calls"], pages=stats["pages"], coverage=stats["coverage"])
        log.info("job_collection_summary", extra={"event": "job_collection_summary", "cloud_service": "mrs",
            "tenant_id": ctx.tenant_id, "project_ref": ctx.project_ref, **stats})
        ctx.stats["jobs_duration_unit_unverified"] = stats["elapsed_unit_unverified"]
        return obs
