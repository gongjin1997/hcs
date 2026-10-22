import time

from app.hcs import HcsError
from app.hcs.pager import page_all

from .base import Observation, RunContext


class DataArtsRealtimeCollector:
    """DataArts Studio 实时作业巡检：作业列表（jobType=REAL_TIME）+ 逐个查询作业状态与节点状态。"""

    code, service, name = "dgc_realtime_status", "dataarts", "DataArts 实时作业状态"

    def collect(self, ctx: RunContext) -> list[Observation]:
        obs: list[Observation] = []
        for ws in ctx.workspaces or [""]:
            headers = {"workspace": ws} if ws else None

            def fetch_jobs(p: dict, h: dict | None = headers) -> dict:
                return ctx.client.get("dataarts", "/v1/{project_id}/jobs", params={**p, "jobType": "REAL_TIME"}, headers=h)

            try:
                jobs = list(page_all(fetch_jobs, "jobs"))
            except HcsError as e:
                ctx.errors.append(f"workspace={ws}: {e}")
                continue
            for j in jobs:
                name = j["name"]
                try:
                    st = ctx.client.get("dataarts", f"/v1/{{project_id}}/jobs/{name}/status", headers=headers)
                except HcsError as e:
                    ctx.errors.append(f"job={name}: {e}")
                    continue
                rid, status = f"{ws}/{name}" if ws else name, st.get("status")
                nodes = st.get("nodes") or []
                bad = [n["name"] for n in nodes if n.get("status") in ("EXCEPTION", "STOPPED")]
                attrs = {"workspace": ws, "owner": j.get("owner"), "priority": j.get("priority"),
                         "path": j.get("path"), "nodes": [{"name": n.get("name"), "status": n.get("status")}
                                                          for n in nodes]}
                obs.append(Observation("dgc_realtime_job", rid, name, "job_status",
                                       {"status": status, "bad_nodes": len(bad)}, status_raw=status, attrs=attrs,
                                       metrics={"nodes": len(nodes), "bad_nodes": len(bad)}))
                for n in nodes:
                    obs.append(Observation("dgc_realtime_job", rid, name, "node_status",
                                           {"job_status": status, "node_status": n.get("status")},
                                           sub_object=n.get("name", "")))
        return obs


class DataArtsBatchCollector:
    """DataArts Studio 批作业巡检：查询最近 24 小时的作业实例，按作业取最新实例。"""

    code, service, name = "dgc_batch_instance", "dataarts", "DataArts 批作业实例"

    def collect(self, ctx: RunContext) -> list[Observation]:
        now_ms = int(time.time() * 1000)
        obs: list[Observation] = []
        for ws in ctx.workspaces or [""]:
            headers = {"workspace": ws} if ws else None
            params = {"minPlanTime": now_ms - 24 * 3600 * 1000, "maxPlanTime": now_ms}

            def fetch_instances(p: dict, h: dict | None = headers, q: dict = params) -> dict:
                return ctx.client.get("dataarts", "/v1/{project_id}/jobs/instances/detail", params={**q, **p}, headers=h)

            try:
                insts = list(page_all(fetch_instances, "instances"))
            except HcsError as e:
                ctx.errors.append(f"workspace={ws}: {e}")
                continue
            latest: dict[str, dict] = {}
            for i in insts:
                if i["jobName"] not in latest or i.get("planTime", 0) > latest[i["jobName"]].get("planTime", 0):
                    latest[i["jobName"]] = i
            for name, i in latest.items():
                start, end = i.get("startTime"), i.get("endTime") or now_ms
                run_min = round((end - start) / 60000, 1) if start else 0
                facts = {"last_status": i.get("status"), "run_minutes": run_min,
                         "p90_minutes": i.get("p90_minutes", 30)}  # P90 基线由平台按历史实例计算后注入
                obs.append(Observation("dgc_batch_job", f"{ws}/{name}" if ws else name, name, "instance_status",
                                       facts, status_raw=i.get("status"),
                                       attrs={"workspace": ws, "instance_id": i.get("instanceId")},
                                       metrics={"run_minutes": run_min}))
        return obs
