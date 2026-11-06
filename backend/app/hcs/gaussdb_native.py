"""GaussDB 25.1.30 API 参考：原生诊断 URI 不继承实例列表的 v3.3 版本。

§4.2.1 使用正式 URI /gaussdb/v3/.../volume-usage；不按示例省略产品前缀，
不借 AOM/CES 查询代替原生数据，不发扩容、终止会话或执行任意 SQL 请求。
"""
import re
from urllib.parse import quote

from app.hcs import HcsError
from app.metrics.dcs_samples import finite_number


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}", value):
        raise ValueError("资源标识格式不正确")
    return quote(value, safe="")


def instance_path(instance_id: str, suffix: str) -> str:
    return "/gaussdb/v3/{project_id}/instances/" + identifier(instance_id) + "/" + suffix


def storage_percent(used: float, total: float) -> float | None:
    value = finite_number(used / total * 100) if total > 0 else None
    return round(value, 2) if value is not None else None


# §4.3.10 表 4-80 组件状态；Down 为宕机，其余为主 / 备 / 正常 / ETCD 角色
COMPONENT_STATES = {"Primary", "Normal", "Down", "Standby", "StateFollower", "StateLeader", "StateCandidate"}
MAX_COMPONENT_PAGES = 20


def components(client, instance_id: str) -> list[dict]:
    """§4.3.10 查询实例的组件列表（分页 offset/limit≤100，total_count 为节点总数）。

    返回扁平的组件清单：node_id、node_name、availability_zone_id、component_id、component_type、role、status、
    distributed_id。结构不完整、分页矛盾或重复时整体报错，不返回半份结果。"""
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    seen_nodes: set[str] = set()
    offset = 0
    for _ in range(MAX_COMPONENT_PAGES):
        body = client.get("gaussdb", instance_path(instance_id, "components"), params={"offset": offset, "limit": 100})
        if not isinstance(body, dict) or not isinstance(body.get("nodes"), list):
            raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "组件列表结构不完整")
        rows, total = body["nodes"], body.get("total_count")
        total = total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else None
        if not rows:
            if total is not None and offset < total:
                raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "组件分页提前结束")
            return result
        if total is not None and offset + len(rows) > total:
            raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "组件总数与记录不一致")
        for row in rows:
            nested = row.get("components") if isinstance(row, dict) else None
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not isinstance(nested, list):
                raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "节点组件结构不完整")
            if row["id"] in seen_nodes:
                raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "组件节点分页重复")
            seen_nodes.add(row["id"])
            for item in nested:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("type"), str):
                    raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "组件标识或类型不完整")
                pair = (row["id"], item["id"])
                if pair in seen:
                    raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "组件列表重复")
                seen.add(pair)
                result.append({"node_id": row["id"], "node_name": _text(row.get("name")),
                               "availability_zone_id": _text(row.get("availability_zone_id")),
                               "component_id": item["id"], "component_type": item["type"],
                               "role": _text(item.get("role")), "status": _text(item.get("status")),
                               "distributed_id": _text(item.get("distributed_id"))})
        offset += len(rows)
        if (total is not None and offset >= total) or (total is None and len(rows) < 100):
            return result
    raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "组件记录过多，请核对接口分页")


def _text(value: object, limit: int = 128) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def volume_usage(client, instance_id: str) -> tuple[float, float, float | None]:
    body = client.get("gaussdb", instance_path(instance_id, "volume-usage"))
    if not isinstance(body, dict):
        raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "GaussDB 磁盘用量响应不完整")
    used, total = finite_number(body.get("used")), finite_number(body.get("total"))
    if used is None or total is None:
        raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "GaussDB 磁盘用量或容量无效")
    percent = storage_percent(used, total)
    if total > 0 and percent is None:
        raise HcsError(0, "LOCAL.GAUSSDB_RESPONSE", "GaussDB 磁盘使用率数值无效")
    return used, total, percent
