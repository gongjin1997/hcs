"""HCS 云监控（CES）查询：批量查询指标数据（batch-query-metric-data）、查询指标列表（metrics）。"""
import json
import math
import re
import time

from app.hcs import HcsClient, HcsError
from app.hcs.pager import response_items

BATCH = 500   # batch-query-metric-data 单次最多 500 个指标


def metric_key(raw: object) -> str:
    """CES 描述符的稳定标识；不再借用已停用的 AOM 适配器。"""
    if not isinstance(raw, dict):
        raise HcsError(0, "LOCAL.CES_RESPONSE", "CES 指标描述符格式不正确")
    namespace, name, dimensions = raw.get("namespace"), raw.get("metric_name"), raw.get("dimensions")
    if (not isinstance(namespace, str) or not namespace or len(namespace) > 255
            or not isinstance(name, str) or not name or len(name) > 255
            or not isinstance(dimensions, list) or not 1 <= len(dimensions) <= 32):
        raise HcsError(0, "LOCAL.CES_RESPONSE", "CES 指标描述符格式不正确")
    dims = []
    for item in dimensions:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,254}", item["name"])
                or not isinstance(item.get("value"), str) or not item["value"] or len(item["value"]) > 1024):
            raise HcsError(0, "LOCAL.CES_RESPONSE", "CES 指标维度格式不正确")
        dims.append({"name": item["name"], "value": item["value"]})
    if len({item["name"] for item in dims}) != len(dims):
        raise HcsError(0, "LOCAL.CES_RESPONSE", "CES 指标维度重复")
    return json.dumps({"namespace": namespace, "metric_name": name, "dimensions": sorted(dims, key=lambda d: d["name"])},
                      sort_keys=True, separators=(",", ":"))


def ces_query(client: HcsClient, reqs: list[dict], window_sec: int = 600, period: str = "1",
              filter_: str = "average") -> list[tuple[dict[str, str], str, float]]:
    """批量查询时间窗内的最新值。reqs = [{"namespace", "metric_name", "dimensions": [{"name", "value"}]}]，
    返回 [(维度 {name: value}, 指标名, 值)]。"""
    out: list[tuple[dict[str, str], str, float]] = []
    reqs = list({json.dumps(row, sort_keys=True, separators=(",", ":")): row for row in reqs}.values())
    if len(reqs) > BATCH * 20:
        raise HcsError(0, "LOCAL.REQUEST_BUDGET", "CES 指标数量超过单次查询预算，未发出查询")
    now_ms = int(time.time() * 1000)
    for start in range(0, len(reqs), BATCH):
        batch = reqs[start:start + BATCH]
        allowed = set()
        for requested in batch:
            try:
                allowed.add(metric_key(requested))
            except HcsError:
                continue
        body = {"metrics": batch, "from": now_ms - window_sec * 1000, "to": now_ms,
                "period": period, "filter": filter_}
        data = client.post("ces", "/V1.0/{project_id}/batch-query-metric-data", json=body)
        for m in response_items(data, "metrics"):
            # 历史响应可未回显 namespace；只在请求的名称和完整维度唯一匹配时归属，不能猜命名空间。
            candidates = [m] if "namespace" in m else [
                {**m, "namespace": requested.get("namespace")} for requested in batch
                if requested.get("metric_name") == m.get("metric_name")]
            matched = set()
            for candidate in candidates:
                try:
                    key = metric_key(candidate)
                except HcsError:
                    continue
                if key in allowed:
                    matched.add(key)
            if len(matched) != 1:
                continue
            points = [p for p in m.get("datapoints") or [] if isinstance(p, dict) and filter_ in p
                      and isinstance(p.get("timestamp"), int | float) and not isinstance(p.get("timestamp"), bool)
                      and math.isfinite(p["timestamp"]) and body["from"] <= p["timestamp"] <= now_ms]
            dimensions = m.get("dimensions")
            if not points or not isinstance(dimensions, list) or not dimensions:
                continue
            last = max(points, key=lambda p: p.get("timestamp", 0))
            raw = last[filter_]
            if raw is None or isinstance(raw, bool):
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(value) or not isinstance(m.get("metric_name"), str):
                continue
            if any(not isinstance(d, dict) or not isinstance(d.get("name"), str)
                   or not isinstance(d.get("value"), str) for d in dimensions):
                continue
            dims = {d["name"]: d["value"] for d in dimensions}
            out.append((dims, m["metric_name"], value))
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
    if not 1 <= limit <= 1000 or max_pages < 1:
        raise ValueError("CES 分页大小或页数不正确")
    start: str | None = None
    seen = set()
    for _ in range(min(max_pages, 20)):
        params: dict = {"namespace": namespace, "metric_name": metric_name, "limit": limit}
        if start:
            params["start"] = start
        data = client.get("ces", "/V1.0/{project_id}/metrics", params=params)
        rows = response_items(data, "metrics")
        out.extend(rows)
        metadata = data.get("meta_data")
        if metadata is not None and not isinstance(metadata, dict):
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "CES 分页元数据格式不正确")
        start = (metadata or {}).get("marker")
        if not start:
            return out
        if not isinstance(start, str) or start in seen:
            raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "CES 指标列表分页标记无效或重复")
        seen.add(start)
    raise HcsError(0, "LOCAL.INCOMPLETE_PAGE", "CES 指标列表超过分页上限，未取得完整清单")


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
