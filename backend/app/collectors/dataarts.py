import logging
import math
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from urllib.parse import quote

from app.hcs import HcsError
from app.hcs.pager import page_all, response_total
from app.services.job_selection import scope_for_context

from .base import Observation, RunContext, log_collection_failure, previous_attrs

log = logging.getLogger(__name__)
REALTIME_STATES = frozenset(("STARTING", "NORMAL", "EXCEPTION", "STOPPING", "STOPPED"))
BATCH_STATES = frozenset(("waiting", "running", "success", "fail", "running-exception", "pause", "manual-stop"))


def dataarts_error(message: str) -> HcsError:
    """DataArts 响应不符合上传 PDF 契约（实时 / 批 / MRS 作业采集器共用）。"""
    return HcsError(0, "LOCAL.DATAARTS_RESPONSE", message)


def millis(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        if not value.isdecimal() or len(value) > 19:
            return None
        value = int(value)
    if isinstance(value, (int, float)) and 0 <= value <= 2**63 - 1 and math.isfinite(value) and value == int(value):
        return int(value)
    return None


def text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _display_time(value: object) -> str | int | float | None:
    if isinstance(value, str):
        return value or None  # 实时状态表为 Date，不能强行按批实例 Long 毫秒解释。
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 2**63 - 1 and math.isfinite(value):
        return value
    return None


def paged_list(fetch: Callable[[dict], dict], key: str) -> list[dict]:
    # EDOC1100450764 的两个列表都把 offset 定义为从 0 开始的页号，而非记录数。
    expected_total: int | None = None

    def complete(params: dict) -> dict:
        nonlocal expected_total
        body = fetch(params)
        if not isinstance(body, dict) or body.get("total") is None or body.get("error_code"):
            raise dataarts_error("DataArts 列表缺少必需的 total，未使用不完整清单")
        total = response_total(body, "total", required=True)
        if expected_total is not None and total != expected_total:
            raise dataarts_error("DataArts 分页 total 发生变化，未使用不完整清单或计算基线")
        expected_total = total
        return body
    return list(page_all(complete, key, mode="page", start=0, limit=100))


DEFINITION_ENGINES = {
    "FlinkSQL": "Flink", "FlinkJar": "Flink", "MRSFlinkJob": "Flink", "HiveSQL": "Hive",
    "SparkSQL": "Spark", "MRSSpark": "Spark", "MapReduce": "MapReduce", "MRSHetuEngine": "HetuEngine",
    "DWSSQL": "DWS",
}


def _list_jobs(ctx: RunContext, workspace: str, job_type: str) -> list[dict]:
    headers = {"workspace": workspace} if workspace else None

    def fetch(params: dict) -> dict:
        body = ctx.client.get("dataarts", "/v1/{project_id}/jobs", params={**params, "jobType": job_type}, headers=headers)
        _page_log(ctx, workspace, "job_definitions", body, "jobs", params, job_type=job_type)
        return body

    jobs = paged_list(fetch, "jobs")
    if any(not isinstance(job.get("name"), str) or not job["name"]
           or job.get("jobType") not in (None, job_type) for job in jobs):
        raise dataarts_error("DataArts 作业清单包含无效名称或不匹配的 jobType")
    if len({job["name"] for job in jobs}) != len(jobs):
        raise dataarts_error("DataArts 作业清单包含重复作业，未使用不完整清单")
    return [{**job, "jobType": job_type} for job in jobs]


def _page_log(ctx: RunContext, workspace: str, phase: str, body: object, key: str, params: dict, **extra) -> None:
    data = body if isinstance(body, dict) else {}
    total = data.get("total")
    log.info("job_collection_page", extra={
        "event": "job_collection_page", "cloud_service": "dataarts", "phase": phase,
        "tenant_id": ctx.tenant_id, "project_ref": ctx.project_ref,
        "workspace": workspace, "workspace_mode": "explicit" if workspace else "default",
        "page": params.get("offset"), "page_size": params.get("limit"),
        "response_total": total if type(total) is int else None,
        "returned_count": len(data[key]) if isinstance(data.get(key), list) else None,
        **extra,
    })


def _summary(ctx: RunContext, workspace: str, **values) -> None:
    entry = {"workspace": workspace, "workspace_mode": "explicit" if workspace else "default", **values}
    ctx.stats.setdefault("dataarts", {"workspaces": []})["workspaces"].append(entry)
    log.info("job_collection_summary", extra={"event": "job_collection_summary", "cloud_service": "dataarts",
             "tenant_id": ctx.tenant_id, "project_ref": ctx.project_ref, **entry})


def _definition_metadata(ctx: RunContext, workspace: str, jobs: list[dict], resource_type: str) -> dict[str, dict]:
    """Read the documented definition fields; never infer an engine from a job name or processing mode."""
    ids = [f"{workspace}/{job['name']}" if workspace else job["name"] for job in jobs]
    cached = previous_attrs(ctx, resource_type, ids, "definition_metadata")
    headers = {"workspace": workspace} if workspace else None

    def fetch(job: dict) -> tuple[str, dict, HcsError | None]:
        name = job["name"]
        rid = f"{workspace}/{name}" if workspace else name
        revision = [job.get("id"), job.get("jobType"), job.get("lastUpdateTime"), job.get("version")]
        prior = cached.get(rid, {})
        if (millis(job.get("lastUpdateTime")) not in (None, 0) and prior.get("revision") == revision
                and prior.get("status") == "AVAILABLE"):
            return name, dict(prior), None
        try:
            body = ctx.client.get("dataarts", f"/v1/{{project_id}}/jobs/{quote(name, safe='')}", headers=headers)
            if (not isinstance(body, dict) or body.get("error_code")
                    or body.get("name") != name
                    or body.get("processType") != job.get("jobType")):
                raise dataarts_error("DataArts 作业定义与请求不匹配")
            nodes = body.get("nodes", [])
            if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
                raise dataarts_error("DataArts 作业定义节点结构无效")
            single = text_or_none(body.get("singleNodeJobType"))
            node_types = sorted({node["type"] for node in nodes if text_or_none(node.get("type"))})
            engines = sorted({DEFINITION_ENGINES[kind] for kind in [single, *node_types] if kind in DEFINITION_ENGINES})
            return name, {"status": "AVAILABLE", "revision": revision, "single_node_type": single,
                          "node_types": node_types, "engines": engines}, None
        except HcsError as error:
            return name, {"status": "FAILED", "revision": revision, "single_node_type": None,
                          "node_types": [], "engines": []}, error

    concurrency = max(1, min(16, int(ctx.params.get("hcs_concurrency", 4))))
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="dataarts-definition") as pool:
        results = [future.result() for future in [pool.submit(copy_context().run, fetch, job) for job in jobs]]
    failed = 0
    for name, _, error in results:
        if error is not None:
            ctx.errors.append(f"workspace={workspace} job={name}: 定义元数据查询失败，未推断执行引擎")
            failed += 1
            if failed <= 5:
                log_collection_failure(log, ctx, error, cloud_service="dataarts", phase="job_definition",
                                       workspace=workspace, resource_name=name)
    return {name: meta for name, meta, _ in results}


def _definition_attrs(job: dict, metadata: dict) -> dict:
    engines = metadata.get("engines", [])
    priority = job.get("priority")
    return {"definition_present": True, "data_object": "DEFINITION", "source": "dataarts_definition",
            "job_type": job.get("jobType"), "owner": text_or_none(job.get("owner")), "path": text_or_none(job.get("path")),
            "priority": priority if type(priority) is int and 0 <= priority <= 2 else None,
            "engine": engines[0] if len(engines) == 1 else None, "engines": engines,
            "engine_source": "DATAARTS_DEFINITION", "single_node_type": metadata.get("single_node_type"),
            "node_types": metadata.get("node_types", []), "definition_metadata": metadata}


def _nodes(state: dict, name: str) -> list[dict] | None:
    if not isinstance(state, dict) or state.get("error_code"):
        raise dataarts_error("DataArts 未返回有效作业状态对象")
    if state.get("name") is not None and state["name"] != name:
        raise dataarts_error("DataArts 状态响应的作业名与请求不一致")
    if state.get("status") is not None and not isinstance(state["status"], str):
        raise dataarts_error("DataArts 作业状态字段格式不正确")
    nodes = state.get("nodes")
    if nodes is None:
        return None  # 官方为可选字段；缺失不等于零节点、零异常。
    if not isinstance(nodes, list) or any(not isinstance(n, dict) or not isinstance(n.get("name"), str)
                                         or not n["name"] for n in nodes):
        raise dataarts_error("DataArts 节点列表或节点名称格式不正确")
    if len({n["name"] for n in nodes}) != len(nodes):
        raise dataarts_error("DataArts 状态响应包含重复节点名")
    if any(n.get("status") is not None and not isinstance(n["status"], str) for n in nodes):
        raise dataarts_error("DataArts 节点状态字段格式不正确")
    return nodes


def _run_minutes(instance: dict, now_ms: int) -> float | None:
    start, end = millis(instance.get("startTime")), millis(instance.get("endTime"))
    if start is None or start == 0:
        return None
    if end == 0 or instance.get("endTime") is None:
        end = now_ms if instance.get("status") in ("running", "running-exception") else None
    # executeTime 的字段表与官方样例单位矛盾，不能据此猜测；只使用有效毫秒时间戳。
    return round((end - start) / 60000, 1) if end is not None and end >= start else None


def _successful_durations(instances: list[dict]) -> dict[str, list[float]]:
    samples: dict[str, dict[int, float]] = {}
    for instance in instances:
        if instance.get("status") != "success":
            continue
        start, end = millis(instance.get("startTime")), millis(instance.get("endTime"))
        identity = millis(instance.get("instanceId"))
        if start is None or start <= 0 or end is None or end < start or identity is None:
            continue
        samples.setdefault(instance["jobName"], {})[identity] = (end - start) / 60000
    return {name: sorted(durations.values()) for name, durations in samples.items()}


class DataArtsRealtimeCollector:
    """DataArts Studio 实时作业巡检：作业列表（jobType=REAL_TIME）+ 逐个查询作业状态与节点状态。"""

    code, service, name = "dgc_realtime_status", "dataarts", "DataArts 实时作业状态"

    def collect(self, ctx: RunContext) -> list[Observation]:
        obs: list[Observation] = []
        scope = scope_for_context(ctx, "dgc_realtime_job")
        for ws in dict.fromkeys(ctx.workspaces or [""]):
            if not scope.keep({"workspace": ws}):
                _summary(ctx, ws, job_type="REAL_TIME", reason="WORKSPACE_EXCLUDED", definitions=None, in_scope=0)
                continue
            headers = {"workspace": ws} if ws else None

            try:
                all_jobs = _list_jobs(ctx, ws, "REAL_TIME")
            except HcsError as e:
                ctx.errors.append(f"workspace={ws}: {e}")
                log_collection_failure(log, ctx, e, cloud_service="dataarts", phase="job_list", workspace=ws)
                _summary(ctx, ws, job_type="REAL_TIME", reason="LIST_FAILED", definitions=None, in_scope=None)
                continue
            jobs = [job for job in all_jobs if scope.keep({"workspace": ws, "job": job["name"]})]
            def fetch_status(job: dict, h: dict | None = headers) -> tuple[dict, dict | None, HcsError | None]:
                name = job["name"]
                try:
                    state = ctx.client.get("dataarts", f"/v1/{{project_id}}/jobs/{quote(name, safe='')}/status",
                                           headers=h)
                    _nodes(state, name)
                    return job, state, None
                except HcsError as e:
                    return job, None, e

            # 列表接口未保证含节点状态，仍查询所有明细；仅 HTTP 并发，ORM/观测组装留在主线程。
            concurrency = max(1, min(16, int(ctx.params.get("hcs_concurrency", 4))))
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="dataarts") as pool:
                futures = [pool.submit(copy_context().run, fetch_status, job) for job in jobs]
                states = [future.result() for future in futures]
            metadata = _definition_metadata(ctx, ws, jobs, "dgc_realtime_job")
            failures = 0
            for j, st, error in states:
                if error or st is None:
                    failures += 1
                    ctx.errors.append(f"job={j['name']}: {error}" if error else f"job={j['name']}: 无状态响应")
                    if error is not None and failures <= 5:
                        log_collection_failure(log, ctx, error, cloud_service="dataarts", phase="job_status",
                                               workspace=ws, resource_name=j["name"])
                    st = {}
                name = j["name"]
                rid, status = f"{ws}/{name}" if ws else name, st.get("status")
                nodes = _nodes(st, name)
                if any(n.get("status") is not None and n["status"] not in REALTIME_STATES for n in nodes or []):
                    ctx.errors.append(f"job={name}: 未识别的 DataArts 节点状态，未按正常状态判定")
                measured = nodes is not None and all(n.get("status") in REALTIME_STATES for n in nodes)
                bad = sum(n.get("status") in ("EXCEPTION", "STOPPED") for n in nodes or []) if measured else None
                facts: dict = {"status": status} if status in REALTIME_STATES else {}
                if bad is not None:
                    facts["bad_nodes"] = bad
                if status is not None and status not in REALTIME_STATES:
                    ctx.errors.append(f"job={name}: 未识别的 DataArts 实时状态，未按正常状态判定")
                attrs = {**_definition_attrs({**j, "jobType": "REAL_TIME"}, metadata[name]),
                         "workspace": ws, "job_status": status,
                         "status_query_status": "FAILED" if error is not None else "AVAILABLE",
                         "start_time": _display_time(st.get("startTime")), "end_time": _display_time(st.get("endTime")),
                         "nodes": [{"name": n["name"], "status": n.get("status"), "type": text_or_none(n.get("type"))}
                                   for n in nodes] if nodes is not None else None}
                obs.append(Observation("dgc_realtime_job", rid, name, "job_status",
                                       facts, status_raw=status or "UNKNOWN", attrs=attrs,
                                       metrics={"nodes": len(nodes) if nodes is not None else None, "bad_nodes": bad}))
                for n in nodes or []:
                    if n.get("status") not in REALTIME_STATES:
                        continue
                    node_facts = {"node_status": n["status"]}
                    if status in REALTIME_STATES:
                        node_facts["job_status"] = status
                    obs.append(Observation("dgc_realtime_job", rid, name, "node_status",
                                           node_facts, sub_object=n["name"]))
            if failures:
                log.warning("collection_detail_failures", extra={"event": "collection_detail_failures",
                            "cloud_service": "dataarts", "workspace": ws, "failed_count": failures,
                            "detail_trace_count": min(5, failures)})
            _summary(ctx, ws, job_type="REAL_TIME", definitions=len(all_jobs), in_scope=len(jobs),
                     status_failures=failures, reason=("DEFAULT_WORKSPACE_EMPTY" if not ws and not all_jobs else
                         "EMPTY_DEFINITIONS" if not all_jobs else "SCOPE_FILTERED" if not jobs else "AVAILABLE"))
        return obs


class DataArtsBatchCollector:
    """Collect BATCH definitions and attach their latest execution within a fixed 24-hour window."""

    code, service, name = "dgc_batch_instance", "dataarts", "DataArts 批作业与实例"

    def collect(self, ctx: RunContext) -> list[Observation]:
        minimum = ctx.params.get("p90_min_samples", 5)
        if type(minimum) is not int or minimum < 1:
            raise HcsError(0, "LOCAL.INVALID_CONFIG", "p90_min_samples 须为正整数，默认 5 是本地样本策略")
        now_ms = int(time.time() * 1000)
        obs: list[Observation] = []
        scope = scope_for_context(ctx, "dgc_batch_job")
        for ws in dict.fromkeys(ctx.workspaces or [""]):
            if not scope.keep({"workspace": ws}):
                _summary(ctx, ws, job_type="BATCH", reason="WORKSPACE_EXCLUDED", definitions=None, in_scope=0)
                continue
            headers = {"workspace": ws} if ws else None
            params = {"minPlanTime": now_ms - 24 * 3600 * 1000, "maxPlanTime": now_ms}
            try:
                all_jobs = _list_jobs(ctx, ws, "BATCH")
            except HcsError as e:
                ctx.errors.append(f"workspace={ws}: {e}")
                log_collection_failure(log, ctx, e, cloud_service="dataarts", phase="batch_definitions", workspace=ws)
                _summary(ctx, ws, job_type="BATCH", reason="LIST_FAILED", definitions=None, in_scope=None)
                continue
            jobs = [job for job in all_jobs if scope.keep({"workspace": ws, "job": job["name"]})]

            def fetch_instances(p: dict, h: dict | None = headers, q: dict = params, workspace: str = ws) -> dict:
                body = ctx.client.get("dataarts", "/v1/{project_id}/jobs/instances/detail", params={**q, **p}, headers=h)
                _page_log(ctx, workspace, "batch_instances", body, "instances", p,
                          window_start_ms=q["minPlanTime"], window_end_ms=q["maxPlanTime"])
                return body

            instance_failed = False
            try:
                insts = paged_list(fetch_instances, "instances")
                if any(not isinstance(i.get("jobName"), str) or not i["jobName"]
                       or millis(i.get("planTime")) is None or not isinstance(i.get("status"), str)
                       or millis(i.get("instanceId")) is None for i in insts):
                    raise dataarts_error("DataArts 实例清单的 jobName、planTime、instanceId 或 status 不完整")
                if len({(i["jobName"], millis(i["instanceId"])) for i in insts}) != len(insts):
                    raise dataarts_error("DataArts 实例分页包含重复实例，未使用不完整清单")
            except HcsError as e:
                ctx.errors.append(f"workspace={ws}: {e}")
                log_collection_failure(log, ctx, e, cloud_service="dataarts", phase="batch_instances", workspace=ws)
                insts, instance_failed = [], True
            raw_count = len(insts)
            insts = [i for i in insts if scope.keep({"workspace": ws, "job": i["jobName"]})]
            samples = _successful_durations(insts)
            latest: dict[str, dict] = {}

            def order(i: dict) -> tuple[int, int, int]:
                return (millis(i.get("planTime")) or 0, millis(i.get("submitTime")) or 0,
                        millis(i.get("instanceId")) or 0)

            for i in insts:
                if i["jobName"] not in latest or order(i) > order(latest[i["jobName"]]):
                    latest[i["jobName"]] = i
            definitions = {job["name"]: job for job in jobs}
            metadata = _definition_metadata(ctx, ws, jobs, "dgc_batch_job")
            for name in dict.fromkeys([*definitions, *latest]):
                job, instance = definitions.get(name), latest.get(name)
                i = instance or {}
                state = i.get("status")
                run_min = _run_minutes(i, now_ms)
                facts: dict = {"last_status": state} if state in BATCH_STATES else {}
                if state is not None and state not in BATCH_STATES:
                    ctx.errors.append(f"job={name}: 未识别的 DataArts 实例状态，未按成功状态判定")
                if run_min is not None:
                    facts["run_minutes"] = run_min
                durations = samples.get(name, [])
                baseline = durations[(9 * len(durations) + 9) // 10 - 1] if len(durations) >= minimum else None
                metrics = {"run_minutes": run_min}
                if baseline is not None:
                    facts["p90_minutes"] = metrics["p90_minutes"] = baseline
                attrs = (_definition_attrs({**job, "jobType": "BATCH"}, metadata[name]) if job is not None else
                         {"definition_present": False, "data_object": "INSTANCE", "source": "dataarts_instance",
                          "job_type": None, "engine": None, "engines": [], "engine_source": None,
                          "single_node_type": None, "node_types": [], "definition_metadata": None})
                attrs.update({"workspace": ws, "instance_id": i.get("instanceId"),
                              "instance_status": state, "plan_time": i.get("planTime"),
                              "instance_type": i.get("instanceType"), "schedule_status": job.get("status") if job else None,
                              "listed_last_instance_status": job.get("lastInstanceStatus") if job else None,
                              "listed_last_instance_end_time": millis(job.get("lastInstanceEndTime")) if job else None,
                              "instance_query_status": "FAILED" if instance_failed else "AVAILABLE" if instance else "EMPTY",
                              "window_start_ms": params["minPlanTime"], "window_end_ms": params["maxPlanTime"],
                              "start_time": millis(i.get("startTime")) or None,
                              "end_time": millis(i.get("endTime")) or None, "submitted_time": millis(i.get("submitTime")),
                              "batch_baseline": {"window_start_ms": params["minPlanTime"],
                                  "window_end_ms": params["maxPlanTime"], "sample_count": len(durations),
                                  "min_samples": minimum, "method": "NEAREST_RANK_P90",
                                  "status": "AVAILABLE" if baseline is not None else "INSUFFICIENT_SAMPLES",
                                  "source": "LOCAL_SUCCESS_INSTANCES"}})
                obs.append(Observation("dgc_batch_job", f"{ws}/{name}" if ws else name, name, "instance_status",
                                       facts, status_raw=state or "UNKNOWN", attrs=attrs, metrics=metrics))
            _summary(ctx, ws, job_type="BATCH", definitions=len(all_jobs), in_scope=len(jobs),
                     instances=None if instance_failed else raw_count, instances_in_scope=len(insts),
                     unmatched_instance_jobs=len(set(latest) - set(definitions)),
                     window_start_ms=params["minPlanTime"], window_end_ms=params["maxPlanTime"],
                     reason=("INSTANCE_QUERY_FAILED" if instance_failed else
                         "DEFAULT_WORKSPACE_EMPTY" if not ws and not all_jobs and not insts else
                         "EMPTY_DEFINITIONS" if not all_jobs else "SCOPE_FILTERED" if not jobs else
                         "NO_RECENT_INSTANCES" if not insts else "AVAILABLE"))
        return obs
