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
