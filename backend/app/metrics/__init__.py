"""监控指标来源。HCS 8.5.1 局点不一定部署了云监控（CES），平台按租户选择指标来源，并自动探测、自动降级：

- CES：HCS 云监控（SYS.ECS / SYS.DCS / SYS.GAUSSDBV5 ...）。ECS 的内存、磁盘使用率需要云服务器安装 CES Agent（AGT.ECS）
- PROMETHEUS：租户侧已有的 Prometheus（node_exporter），CES 不可用时提供 ECS 的 CPU / 内存 / 磁盘 / 开机时间
- NONE：只做状态类巡检（实例状态、DCS 内存、GaussDB 节点等来自各服务自身 API 的数据不受影响）

租户 metrics_source = AUTO（默认）：除非已探测到“CES 不可用”，否则调用 CES；连通性测试会探测 CES，
巡检时 CES 返回 404 / 501 或域名无法解析也会把租户标记为“CES 不可用”，之后改用 Prometheus（已配置时）或只巡检状态。
"""
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

from app.hcs import HcsError
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.collectors.base import RunContext

T = TypeVar("T")

SOURCES = {"AUTO": "自动探测", "CES": "云监控 CES", "PROMETHEUS": "Prometheus", "NONE": "不采集指标"}
CAPABILITIES = {"ces": "云监控 CES", "prometheus": "Prometheus", "cce_api": "CCE K8s API"}
_DNS_ERRORS = ("Name or service not known", "nodename nor servname", "getaddrinfo", "No address associated")


def _caps(tenant: object) -> dict:
    return dict(getattr(tenant, "capabilities", None) or {})


def available(tenant: object, name: str) -> bool | None:
    """能力探测结果：True / False；None 表示还没探测过。"""
    return (_caps(tenant).get(name) or {}).get("available")


def mode(tenant: object) -> str:
    return str(getattr(tenant, "metrics_source", None) or "AUTO").upper()


def use_ces(tenant: object) -> bool:
    m = mode(tenant)
    if m == "CES":
        return True
    return m == "AUTO" and available(tenant, "ces") is not False


def use_prometheus(tenant: object) -> bool:
    if tenant is None or not getattr(tenant, "prometheus_url", None):
        return False
    m = mode(tenant)
    return m == "PROMETHEUS" or (m == "AUTO" and available(tenant, "ces") is False)


def effective(tenant: object) -> str:
    """当前生效的指标来源：CES / PROMETHEUS / NONE（页面展示、巡检记录用）。"""
    if use_ces(tenant):
        return "CES"
    return "PROMETHEUS" if use_prometheus(tenant) else "NONE"


def mark(tenant: object, name: str, ok: bool, reason: str = "", **extra: object) -> None:
    """记录能力探测结果（只在结果变化或首次探测时写入，避免每轮巡检都更新租户记录）。"""
    if tenant is None:
        return
    caps = _caps(tenant)
    old = caps.get(name) or {}
    if old.get("available") is ok and old.get("reason", "") == reason and not extra:
        return
    caps[name] = {"available": ok, "reason": reason, "at": utcnow().isoformat(timespec="seconds"), **extra}
    tenant.capabilities = caps  # type: ignore[attr-defined]


def service_missing(e: HcsError) -> bool:
    """错误是否表示“局点没有这个服务”（而不是偶发故障）：404 / 501，或服务域名无法解析。"""
    if e.status in (404, 501):
        return True
    return e.status == 0 and any(x in e.message for x in _DNS_ERRORS)


def guarded_ces(ctx: "RunContext", fn: "Callable[[], T]", empty: "T") -> "T":
    """按租户设置调用 CES：未启用或局点没有 CES 时返回 empty（并标记租户“CES 不可用”，不算巡检局部失败）；
    其他错误记入 ctx.errors（任务记为 PARTIAL），状态类巡检照常进行。"""
    if not use_ces(ctx.tenant):
        return empty
    try:
        result = fn()
    except HcsError as e:
        if service_missing(e):
            mark(ctx.tenant, "ces", False, f"巡检调用 CES 返回 {e.status or '网络错误'}：{e.message[:120]}")
        else:
            ctx.errors.append(f"CES: {e}")
        return empty
    if available(ctx.tenant, "ces") is not True:
        mark(ctx.tenant, "ces", True, "巡检调用成功")
    return result
