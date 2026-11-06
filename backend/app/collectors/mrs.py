import logging
import math
import time
from urllib.parse import quote

from app.hcs import HcsError
from app.hcs.pager import page_all

from .base import Observation, RunContext, log_collection_failure

log = logging.getLogger(__name__)
CLUSTER_STATES = frozenset(("starting", "running", "terminated", "failed", "abnormal", "terminating",
                            "frozen", "scaling-out", "scaling-in"))


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


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


class MrsClusterCollector:
    code, service, name = "mrs_cluster_status", "mrs", "MRS 集群状态"

    def collect(self, ctx: RunContext) -> list[Observation]:
        observations = []
        for cluster in _clusters(ctx):
            state = cluster.get("clusterState")
            known = state in CLUSTER_STATES
            if not known:
                ctx.errors.append(f"cluster={cluster['clusterId']}: MRS 集群状态缺失或未被上传 PDF 确认，未按正常状态判定")
            observations.append(Observation(
                "mrs_cluster", cluster["clusterId"], cluster["clusterName"], "cluster_status",
                {"cluster_state": state} if known else {}, status_raw=state or "UNKNOWN",
                attrs={"version": cluster.get("clusterVersion"), "nodes": cluster.get("totalNodeNum")}))
        return observations


class MrsJobCollector:
    """V2 作业兼容适配；HCS 8.5.1 的公开 API 目录未列出此接口，不推定局点时长单位。"""

    code, service, name = "mrs_job_status", "mrs", "MRS 作业执行"

    def collect(self, ctx: RunContext) -> list[Observation]:
        hours = _number(ctx.params.get("window_hours", 6))
        if hours is None or hours <= 0 or hours > 31 * 24:
            raise HcsError(0, "LOCAL.INVALID_CONFIG", "MRS 作业查询窗口须大于零且不超过 31 天")
        since = int(time.time() * 1000 - hours * 3600 * 1000)
        # 仅显式配置或开发 Mock 的合成契约可确认单位，不能套用公有云文档到 HCS 局点。
        unit = ctx.params.get("elapsed_time_unit")
        if unit is None and getattr(ctx.client, "mock_url", None):
            unit = "milliseconds"
        if unit not in (None, "milliseconds", "seconds"):
            raise HcsError(0, "LOCAL.INVALID_CONFIG", "MRS elapsed_time_unit 须为 milliseconds 或 seconds")
        mode = ctx.params.get("job_offset_mode", "page")
        if mode not in ("page", "offset"):
            raise HcsError(0, "LOCAL.INVALID_CONFIG", "MRS job_offset_mode 须为 page 或 offset")
        ctx.stats.update(job_api_contract="unverified_hcs851_compatibility", elapsed_time_unit=unit,
                         job_offset_mode=mode)
        obs: list[Observation] = []
        reported: set[str] = set()
        duration_unit_unknown = 0
        for c in _clusters(ctx):
            cid = c["clusterId"]

            def fetch(p: dict, cid: str = cid) -> dict:
                # 记录偏移从 1 起时，仅在发送时加 1；共享 pager 的计数仍从 0 起，不能少读最后一条。
                params = {**p, "offset": p["offset"] + (1 if mode == "offset" else 0),
                          "submitted_time_begin": since}
                data = ctx.client.get("mrs", f"/v2/{{project_id}}/clusters/{quote(cid, safe='')}/job-executions",
                                      params=params)
                if "total_record" not in data or data["total_record"] is None:
                    raise HcsError(0, "LOCAL.INVALID_RESPONSE", "MRS 作业总数缺失，未使用不完整清单")
                return data

            try:
                jobs = list(page_all(fetch, "job_list", mode=mode, start=0 if mode == "offset" else 1,
                                     total_key="total_record"))
                latest: dict[str, dict] = {}
                submitted_times: dict[str, float] = {}
                execution_ids: set[str] = set()
                for job in jobs:
                    name = job.get("job_name")
                    submitted = _number(job.get("submitted_time"))
                    if not isinstance(name, str) or not name or submitted is None:
                        raise HcsError(0, "LOCAL.INVALID_RESPONSE", "MRS 作业名称或提交时间缺失，未使用不完整清单")
                    execution_id = job.get("job_id")
                    if isinstance(execution_id, str) and execution_id:
                        if execution_id in execution_ids:
                            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "MRS 作业分页包含重复执行，未使用不完整清单")
                        execution_ids.add(execution_id)
                    if name not in latest or submitted > submitted_times[name]:
                        latest[name] = job
                        submitted_times[name] = submitted
            except HcsError as e:
                ctx.errors.append(f"cluster={c.get('clusterName')}: {e}")
                log_collection_failure(log, ctx, e, cloud_service="mrs", phase="job_executions", cluster_id=cid)
                continue
            for name, j in latest.items():
                raw_elapsed = _number(j.get("elapsed_time"))
                elapsed = round(raw_elapsed / (60000 if unit == "milliseconds" else 60), 1) \
                    if raw_elapsed is not None and unit is not None else None
                if j.get("elapsed_time") is not None and raw_elapsed is None and "invalid_elapsed" not in reported:
                    ctx.errors.append("MRS elapsed_time 格式无效，未将其作为零时长")
                    reported.add("invalid_elapsed")
                if raw_elapsed is not None and unit is None and "unknown_unit" not in reported:
                    ctx.errors.append("MRS elapsed_time 单位未经局点确认，未换算运行时长；请核对作业 API 契约")
                    reported.add("unknown_unit")
                if raw_elapsed is not None and unit is None:
                    duration_unit_unknown += 1
                facts = {"job_state": j.get("job_state"), "job_result": j.get("job_result"), "elapsed_min": elapsed}
                obs.append(Observation("mrs_job", f"{cid}/{name}", name, "job_status", facts,
                                       status_raw=j.get("job_state") or j.get("job_result"),
                                       attrs={"cluster": c.get("clusterName"), "job_type": j.get("job_type"),
                                              "cluster_id": cid, "queue": j.get("queue"), "job_id": j.get("job_id"),
                                              "job_state": j.get("job_state"), "job_result": j.get("job_result"),
                                              "submitted_time": j.get("submitted_time"),
                                              "start_time": j.get("started_time", j.get("start_time")),
                                              "end_time": j.get("finished_time", j.get("end_time")),
                                              "elapsed_time": j.get("elapsed_time"), "elapsed_time_unit": unit},
                                       metrics={"elapsed_min": elapsed}))
        ctx.stats["jobs_duration_unit_unverified"] = duration_unit_unknown
        if duration_unit_unknown:
            log.warning("collection_data_unavailable", extra={"event": "collection_data_unavailable",
                        "cloud_service": "mrs", "phase": "job_duration",
                        "reason": "elapsed_time_unit_unverified", "affected_count": duration_unit_unknown,
                        "tenant_id": ctx.tenant_id, "project_ref": ctx.project_ref})
        return obs
