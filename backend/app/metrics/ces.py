"""HCS 云监控（CES）查询：批量查询指标数据（batch-query-metric-data）、查询指标列表（metrics）。"""
import time

from app.hcs import HcsClient

BATCH = 500   # batch-query-metric-data 单次最多 500 个指标


def ces_query(client: HcsClient, reqs: list[dict], window_sec: int = 600, period: str = "1",
              filter_: str = "average") -> list[tuple[dict[str, str], str, float]]:
    """批量查询时间窗内的最新值。reqs = [{"namespace", "metric_name", "dimensions": [{"name", "value"}]}]，
    返回 [(维度 {name: value}, 指标名, 值)]。"""
    out: list[tuple[dict[str, str], str, float]] = []
    now_ms = int(time.time() * 1000)
    for start in range(0, len(reqs), BATCH):
        body = {"metrics": reqs[start:start + BATCH], "from": now_ms - window_sec * 1000, "to": now_ms,
                "period": period, "filter": filter_}
        data = client.post("ces", "/V1.0/{project_id}/batch-query-metric-data", json=body)
        for m in data.get("metrics", []):
            points = [p for p in m.get("datapoints") or [] if filter_ in p]
            if not points or not m.get("dimensions"):
                continue
            last = max(points, key=lambda p: p.get("timestamp", 0))
            dims = {d["name"]: d["value"] for d in m["dimensions"]}
            out.append((dims, m["metric_name"], float(last[filter_])))
    return out


def ces_latest(client: HcsClient, namespace: str, dim_name: str, ids: list[str], metrics: list[str],
               window_sec: int = 600, period: str = "1", filter_: str = "average") -> dict[str, dict[str, float]]:
    """单维度资源的最新指标：返回 {资源ID: {指标名: 值}}。"""
    reqs = [{"namespace": namespace, "metric_name": m, "dimensions": [{"name": dim_name, "value": i}]}
            for i in ids for m in metrics]
    out: dict[str, dict[str, float]] = {}
    for dims, name, value in ces_query(client, reqs, window_sec, period, filter_):
        if dim_name in dims:
            out.setdefault(dims[dim_name], {})[name] = value
    return out


def ces_list_metrics(client: HcsClient, namespace: str, metric_name: str, limit: int = 1000,
                     max_pages: int = 20) -> list[dict]:
    """查询指标列表（用于发现多维度指标，例如 AGT.ECS 磁盘使用率的 instance_id × mount_point）。"""
    out: list[dict] = []
    start: str | None = None
    for _ in range(max_pages):
        params: dict = {"namespace": namespace, "metric_name": metric_name, "limit": limit}
        if start:
            params["start"] = start
        data = client.get("ces", "/V1.0/{project_id}/metrics", params=params)
        rows = data.get("metrics") or []
        out.extend(rows)
        start = (data.get("meta_data") or {}).get("marker")
        if not start or len(rows) < limit:
            break
    return out


def ecs_metrics(client: HcsClient, ids: list[str]) -> dict[str, dict]:
    """ECS 最新使用率：CPU（SYS.ECS cpu_util，无需 Agent）；内存、磁盘（AGT.ECS，需安装 CES Agent，未安装的实例没有数据）。"""
    out: dict[str, dict] = {}
    if not ids:
        return out
    for iid, m in ces_latest(client, "SYS.ECS", "instance_id", ids, ["cpu_util"]).items():
        out.setdefault(iid, {})["cpu_util"] = round(m["cpu_util"], 1)
    for iid, m in ces_latest(client, "AGT.ECS", "instance_id", ids, ["mem_usedPercent"]).items():
        out.setdefault(iid, {})["mem_util"] = round(m["mem_usedPercent"], 1)
    wanted = set(ids)
    disk_reqs = [{"namespace": "AGT.ECS", "metric_name": "disk_usedPercent", "dimensions": r["dimensions"]}
                 for r in ces_list_metrics(client, "AGT.ECS", "disk_usedPercent")
                 if any(d.get("name") == "instance_id" and d.get("value") in wanted for d in r.get("dimensions") or [])]
    for dims, _, value in ces_query(client, disk_reqs):
        cur = out.setdefault(dims.get("instance_id", ""), {})
        if value > cur.get("disk_util", -1):
            cur["disk_util"], cur["disk_mount"] = round(value, 1), dims.get("mount_point", "-")
    out.pop("", None)
    return out
