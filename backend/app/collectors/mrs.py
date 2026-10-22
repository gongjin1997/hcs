import time

from app.hcs import HcsError
from app.hcs.pager import page_all

from .base import Observation, RunContext


def _clusters(ctx: RunContext) -> list[dict]:
    return list(page_all(lambda p: ctx.client.get("mrs", "/v1.1/{project_id}/cluster_infos", params=p), "clusters",
                         mode="page", start=1, total_key="clusterTotal", offset_param="currentPage",
                         limit_param="pageSize", limit=50))


class MrsClusterCollector:
    code, service, name = "mrs_cluster_status", "mrs", "MRS 集群状态"

    def collect(self, ctx: RunContext) -> list[Observation]:
        return [Observation("mrs_cluster", c["clusterId"], c["clusterName"], "cluster_status",
                            {"cluster_state": c.get("clusterState")}, status_raw=c.get("clusterState"),
                            attrs={"version": c.get("clusterVersion"), "nodes": c.get("totalNodeNum")})
                for c in _clusters(ctx)]


class MrsJobCollector:
    """MRS 作业巡检：V2 作业列表（成功时返回 202），按作业名取最近一次执行。"""

    code, service, name = "mrs_job_status", "mrs", "MRS 作业执行"

    def collect(self, ctx: RunContext) -> list[Observation]:
        since = int(time.time() * 1000) - ctx.params.get("window_hours", 6) * 3600 * 1000
        obs: list[Observation] = []
        for c in _clusters(ctx):
            cid = c["clusterId"]

            def fetch(p: dict, cid: str = cid) -> dict:
                return ctx.client.get("mrs", f"/v2/{{project_id}}/clusters/{cid}/job-executions",
                                      params={**p, "submitted_time_begin": since})

            try:   # offset 语义（页码 / 记录偏移）以局点 MRS API 版本为准
                jobs = list(page_all(fetch, "job_list", mode="page", start=1, total_key="total_record"))
            except HcsError as e:
                ctx.errors.append(f"cluster={c.get('clusterName')}: {e}")
                continue
            latest: dict[str, dict] = {}
            for j in jobs:
                if j["job_name"] not in latest or j.get("submitted_time", 0) > latest[j["job_name"]].get(
                        "submitted_time", 0):
                    latest[j["job_name"]] = j
            for name, j in latest.items():
                elapsed = round((j.get("elapsed_time") or 0) / 60000, 1)
                facts = {"job_state": j.get("job_state"), "job_result": j.get("job_result"), "elapsed_min": elapsed}
                obs.append(Observation("mrs_job", f"{cid}/{name}", name, "job_status", facts,
                                       status_raw=j.get("job_result") or j.get("job_state"),
                                       attrs={"cluster": c.get("clusterName"), "job_type": j.get("job_type"),
                                              "queue": j.get("queue"), "job_id": j.get("job_id")},
                                       metrics={"elapsed_min": elapsed}))
        return obs
