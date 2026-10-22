from app.hcs.pager import page_all
from app.ledger.fetchers import gaussdb_attrs
from app.metrics import guarded_ces
from app.metrics.ces import ces_latest

from .base import Observation, RunContext

# CES 命名空间 / 维度 / 指标名因局点与版本而异，以 CES 控制台显示为准，可通过任务参数覆盖
DEFAULT_CES = {"namespace": "SYS.GAUSSDBV5", "dimension": "gaussdbv5_instance_id",
               "metrics": {"cpu_usage": "rds001_cpu_util", "mem_usage": "rds002_mem_util",
                           "disk_usage": "rds039_disk_util"}}


class GaussDBCollector:
    """GaussDB 实例巡检：实例列表（状态、节点）+ CES 指标（CPU、内存、磁盘使用率）。"""

    code, service, name = "gaussdb_status", "gaussdb", "GaussDB 实例状态与容量"

    def collect(self, ctx: RunContext) -> list[Observation]:
        conf = {**DEFAULT_CES, **ctx.params.get("ces", {})}
        insts = list(page_all(lambda p: ctx.client.get("gaussdb", "/v3/{project_id}/instances", params=p),
                              "instances", total_key="total_count"))
        ces: dict[str, dict[str, float]] = guarded_ces(ctx, lambda: ces_latest(ctx.client, conf["namespace"], conf["dimension"],
                                                  [i["id"] for i in insts], list(conf["metrics"].values()),
                                                  window_sec=900, period="300"), {})
        obs: list[Observation] = []
        for i in insts:
            iid, name, status = i["id"], i["name"], i.get("status")
            nodes = [{"name": n.get("name"), "role": n.get("role"), "status": n.get("status")}
                     for n in i.get("nodes", [])]
            attrs = {**gaussdb_attrs(i), "nodes": nodes}
            obs.append(Observation("gaussdb_instance", iid, name, "instance_status", {"status": status},
                                   status_raw=status, attrs=attrs))
            raw = ces.get(iid, {})
            m = {k: round(raw[v], 1) for k, v in conf["metrics"].items() if v in raw}
            for item, key in (("disk", "disk_usage"), ("cpu", "cpu_usage"), ("memory", "mem_usage")):
                if key in m:
                    obs.append(Observation("gaussdb_instance", iid, name, item, {key: m[key]}, metrics={key: m[key]}))
        return obs
