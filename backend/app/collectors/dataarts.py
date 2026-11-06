import logging
import math
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from urllib.parse import quote

from app.hcs import HcsError
from app.hcs.pager import page_all, response_total

from .base import Observation, RunContext, log_collection_failure

log = logging.getLogger(__name__)
REALTIME_STATES = frozenset(("STARTING", "NORMAL", "EXCEPTION", "STOPPING", "STOPPED"))
BATCH_STATES = frozenset(("waiting", "running", "success", "fail", "running-exception", "pause", "manual-stop"))


def _invalid(message: str) -> HcsError:
    return HcsError(0, "LOCAL.DATAARTS_RESPONSE", message)


def _millis(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        if not value.isdecimal() or len(value) > 19:
            return None
        value = int(value)
    if isinstance(value, (int, float)) and 0 <= value <= 2**63 - 1 and math.isfinite(value) and value == int(value):
        return int(value)
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _display_time(value: object) -> str | int | float | None:
    if isinstance(value, str):
        return value or None  # 实时状态表为 Date，不能强行按批实例 Long 毫秒解释。
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 2**63 - 1 and math.isfinite(value):
        return value
    return None


def _pages(fetch: Callable[[dict], dict], key: str) -> list[dict]:
    # EDOC1100450764 的两个列表都把 offset 定义为从 0 开始的页号，而非记录数。
    expected_total: int | None = None

    def complete(params: dict) -> dict:
        nonlocal expected_total
        body = fetch(params)
        if not isinstance(body, dict) or body.get("total") is None or body.get("error_code"):
            raise _invalid("DataArts 列表缺少必需的 total，未使用不完整清单")
        total = response_total(body, "total", required=True)
        if expected_total is not None and total != expected_total:
            raise _invalid("DataArts 分页 total 发生变化，未使用不完整清单或计算基线")
        expected_total = total
        return body
    return list(page_all(complete, key, mode="page", start=0, limit=100))


def _nodes(state: dict, name: str) -> list[dict] | None:
    if not isinstance(state, dict) or state.get("error_code"):
        raise _invalid("DataArts 未返回有效作业状态对象")
    if state.get("name") is not None and state["name"] != name:
        raise _invalid("DataArts 状态响应的作业名与请求不一致")
    if state.get("status") is not None and not isinstance(state["status"], str):
        raise _invalid("DataArts 作业状态字段格式不正确")
    nodes = state.get("nodes")
    if nodes is None:
        return None  # 官方为可选字段；缺失不等于零节点、零异常。
    if not isinstance(nodes, list) or any(not isinstance(n, dict) or not isinstance(n.get("name"), str)
                                         or not n["name"] for n in nodes):
        raise _invalid("DataArts 节点列表或节点名称格式不正确")
    if len({n["name"] for n in nodes}) != len(nodes):
        raise _invalid("DataArts 状态响应包含重复节点名")
    if any(n.get("status") is not None and not isinstance(n["status"], str) for n in nodes):
        raise _invalid("DataArts 节点状态字段格式不正确")
    return nodes


def _run_minutes(instance: dict, now_ms: int) -> float | None:
    start, end = _millis(instance.get("startTime")), _millis(instance.get("endTime"))
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
        start, end = _millis(instance.get("startTime")), _millis(instance.get("endTime"))
        identity = _millis(instance.get("instanceId"))
        if start is None or start <= 0 or end is None or end < start or identity is None:
            continue
        samples.setdefault(instance["jobName"], {})[identity] = (end - start) / 60000
    return {name: sorted(durations.values()) for name, durations in samples.items()}


class DataArtsRealtimeCollector:
    """DataArts Studio 实时作业巡检：作业列表（jobType=REAL_TIME）+ 逐个查询作业状态与节点状态。"""

    code, service, name = "dgc_realtime_status", "dataarts", "DataArts 实时作业状态"

    def collect(self, ctx: RunContext) -> list[Observation]:
        obs: list[Observation] = []
        for ws in dict.fromkeys(ctx.workspaces or [""]):
            headers = {"workspace": ws} if ws else None

            def fetch_jobs(p: dict, h: dict | None = headers) -> dict:
                return ctx.client.get("dataarts", "/v1/{project_id}/jobs", params={**p, "jobType": "REAL_TIME"}, headers=h)

            try:
                jobs = _pages(fetch_jobs, "jobs")
                if any(not isinstance(job.get("name"), str) or not job["name"]
                       or job.get("jobType") not in (None, "REAL_TIME") for job in jobs):
                    raise _invalid("DataArts 实时作业清单包含无效名称或不匹配的 jobType")
                if len({job["name"] for job in jobs}) != len(jobs):
                    raise _invalid("DataArts 实时作业清单包含重复作业，未使用不完整清单")
            except HcsError as e:
                ctx.errors.append(f"workspace={ws}: {e}")
                log_collection_failure(log, ctx, e, cloud_service="dataarts", phase="job_list", workspace=ws)
                continue
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
            failures = 0
            for j, st, error in states:
                if error or st is None:
                    failures += 1
                    ctx.errors.append(f"job={j['name']}: {error}" if error else f"job={j['name']}: 无状态响应")
                    if error is not None and failures <= 5:
                        log_collection_failure(log, ctx, error, cloud_service="dataarts", phase="job_status",
                                               workspace=ws, resource_name=j["name"])
                    continue
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
                priority = j.get("priority")
                attrs = {"workspace": ws, "owner": j.get("owner"),
                         "priority": priority if type(priority) is int and 0 <= priority <= 2 else None,
                         "path": j.get("path"), "job_type": j.get("jobType"), "job_status": status,
                         "engine": _text(j.get("engine")) or _text(j.get("jobEngine")),
                         "start_time": _display_time(st.get("startTime")), "end_time": _display_time(st.get("endTime")),
                         "nodes": [{"name": n["name"], "status": n.get("status"), "type": _text(n.get("type"))}
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
        return obs


class DataArtsBatchCollector:
    """DataArts Studio 批作业巡检：查询最近 24 小时的作业实例，按作业取最新实例。"""

    code, service, name = "dgc_batch_instance", "dataarts", "DataArts 批作业实例"

    def collect(self, ctx: RunContext) -> list[Observation]:
        minimum = ctx.params.get("p90_min_samples", 5)
        if type(minimum) is not int or minimum < 1:
            raise HcsError(0, "LOCAL.INVALID_CONFIG", "p90_min_samples 须为正整数，默认 5 是本地样本策略")
        now_ms = int(time.time() * 1000)
        obs: list[Observation] = []
        for ws in dict.fromkeys(ctx.workspaces or [""]):
            headers = {"workspace": ws} if ws else None
            params = {"minPlanTime": now_ms - 24 * 3600 * 1000, "maxPlanTime": now_ms}

            def fetch_instances(p: dict, h: dict | None = headers, q: dict = params) -> dict:
                return ctx.client.get("dataarts", "/v1/{project_id}/jobs/instances/detail", params={**q, **p}, headers=h)

            try:
                insts = _pages(fetch_instances, "instances")
                if any(not isinstance(i.get("jobName"), str) or not i["jobName"]
                       or _millis(i.get("planTime")) is None or not isinstance(i.get("status"), str)
                       or _millis(i.get("instanceId")) is None
                       for i in insts):
                    raise _invalid("DataArts 实例清单的 jobName、planTime、instanceId 或 status 不完整")
                if len({(i["jobName"], _millis(i["instanceId"])) for i in insts}) != len(insts):
                    raise _invalid("DataArts 实例分页包含重复实例，未使用不完整清单")
            except HcsError as e:
                ctx.errors.append(f"workspace={ws}: {e}")
                log_collection_failure(log, ctx, e, cloud_service="dataarts", phase="batch_instances", workspace=ws)
                continue
            samples = _successful_durations(insts)
            latest: dict[str, dict] = {}
            def order(i: dict) -> tuple[int, int, int]:
                return (_millis(i.get("planTime")) or 0, _millis(i.get("submitTime")) or 0,
                        _millis(i.get("instanceId")) or 0)
            for i in insts:
                if i["jobName"] not in latest or order(i) > order(latest[i["jobName"]]):
                    latest[i["jobName"]] = i
            for name, i in latest.items():
                run_min = _run_minutes(i, now_ms)
                facts: dict = {"last_status": i["status"]} if i["status"] in BATCH_STATES else {}
                if i["status"] not in BATCH_STATES:
                    ctx.errors.append(f"job={name}: 未识别的 DataArts 实例状态，未按成功状态判定")
                if run_min is not None:
                    facts["run_minutes"] = run_min
                durations = samples.get(name, [])
                # nearest-rank ceil(0.9*n)，用整数秩避免浮点乘法边界；原始时长不预先四舍五入。
                baseline = durations[(9 * len(durations) + 9) // 10 - 1] if len(durations) >= minimum else None
                metrics = {"run_minutes": run_min}
                if baseline is not None:
                    facts["p90_minutes"] = metrics["p90_minutes"] = baseline
                obs.append(Observation("dgc_batch_job", f"{ws}/{name}" if ws else name, name, "instance_status",
                                       facts, status_raw=i.get("status"),
                                       attrs={"workspace": ws, "instance_id": i.get("instanceId"),
                                              "job_type": _text(i.get("jobType")),
                                              "engine": _text(i.get("engine")) or _text(i.get("jobEngine")),
                                              "instance_status": i.get("status"), "plan_time": i.get("planTime"),
                                              "instance_type": i.get("instanceType"),
                                              "start_time": _millis(i.get("startTime")) or None,
                                              "end_time": _millis(i.get("endTime")) or None,
                                              "submitted_time": _millis(i.get("submitTime")),
                                              "batch_baseline": {"window_start_ms": params["minPlanTime"],
                                                  "window_end_ms": params["maxPlanTime"], "sample_count": len(durations),
                                                  "min_samples": minimum, "method": "NEAREST_RANK_P90",
                                                  "status": "AVAILABLE" if baseline is not None else "INSUFFICIENT_SAMPLES",
                                                  "source": "LOCAL_SUCCESS_INSTANCES"}}, metrics=metrics))
        return obs
