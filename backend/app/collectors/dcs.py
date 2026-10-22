from app.hcs.pager import page_all
from app.metrics import guarded_ces
from app.metrics.ces import ces_latest

from .base import Observation, RunContext


class DcsCollector:
    """DCS 实例巡检：实例列表（状态、used_memory / max_memory）+ CES（SYS.DCS）CPU、连接数。"""

    code, service, name = "dcs_status", "dcs", "DCS 实例状态与内存"

    def collect(self, ctx: RunContext) -> list[Observation]:
        insts = list(page_all(lambda p: ctx.client.get("dcs", "/v2/{project_id}/instances", params=p),
                              "instances", limit=1000, total_key="instance_num"))
        # CES 可选：局点未部署 / 调用失败时只跳过 CPU、连接数（内存使用率来自 DCS 接口本身）
        ces: dict[str, dict[str, float]] = guarded_ces(ctx, lambda: ces_latest(ctx.client, "SYS.DCS", "dcs_instance_id",
                                                  [i["instance_id"] for i in insts], ["cpu_usage", "connected_clients"]),
                          {})
        obs: list[Observation] = []
        for i in insts:
            iid, name, status = i["instance_id"], i["name"], i.get("status")
            attrs = {k: i.get(k) for k in ("engine", "engine_version", "capacity", "spec_code", "ip", "port",
                                           "max_memory", "max_clients")}
            obs.append(Observation("dcs_instance", iid, name, "instance_status", {"status": status},
                                   status_raw=status, attrs=attrs))
            used, total = i.get("used_memory"), i.get("max_memory")
            if used is not None and total:
                usage = round(used * 100 / total, 1)
                m = {"memory_usage": usage, "used_memory_mb": used, "max_memory_mb": total}
                obs.append(Observation("dcs_instance", iid, name, "memory", m, metrics=m))
            cm = ces.get(iid, {})
            if "cpu_usage" in cm:
                m = {"cpu_usage": round(cm["cpu_usage"], 1)}
                obs.append(Observation("dcs_instance", iid, name, "cpu", m, metrics=m))
            if "connected_clients" in cm:
                m = {"connected_clients": int(cm["connected_clients"]), "max_clients": i.get("max_clients")}
                obs.append(Observation("dcs_instance", iid, name, "connections", m,
                                       metrics={"connected_clients": m["connected_clients"]}))
        return obs
