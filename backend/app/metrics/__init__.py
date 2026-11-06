"""外部指标选源：AOM 全局停用，AUTO 仅选择已配置 Prometheus。

旧 AOM 地址、映射和能力记录仅保留兼容，不参与选源、检测和采样。
CES 仍须全局与租户双重显式启用；各产品原生只读接口独立运行。
"""
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

from app.core.config import get_settings
from app.hcs import HcsError
from app.hcs.readonly import ces_access
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.collectors.base import RunContext

T = TypeVar("T")

SOURCES = {"AUTO": "自动选择", "CES": "云监控 CES（兼容）",
           "PROMETHEUS": "Prometheus", "NONE": "仅服务原生字段"}
CAPABILITIES = {"ces": "云监控 CES（兼容）", "prometheus": "Prometheus", "cce_api": "CCE K8s API"}
_DNS_ERRORS = ("Name or service not known", "nodename nor servname", "getaddrinfo", "No address associated")


def _caps(tenant: object) -> dict:
    return dict(getattr(tenant, "capabilities", None) or {})


def available(tenant: object, name: str) -> bool | None:
    """AOM 始终不可用，旧能力记录不能重新启用它。"""
    if name == "aom":
        return False
    entry = _caps(tenant).get(name)
    if not isinstance(entry, dict):
        return None
    value = entry.get("available")
    return value if isinstance(value, bool) else None


def mode(tenant: object) -> str:
    return str(getattr(tenant, "metrics_source", None) or "AUTO").upper()


def use_ces(tenant: object) -> bool:
    """仅旧部署显式启用且租户显式选择 CES 时允许请求；AUTO 永远不走 CES。"""
    return bool(get_settings().hcs_ces_enabled) and mode(tenant) == "CES"


def aom_configured(tenant: object, *, client: object | None = None) -> bool:
    """旧调用兼容：即使保存了地址，本版本也不支持 AOM。"""
    return False


def use_aom(tenant: object, *, client: object | None = None) -> bool:
    """旧调用兼容，所有资源均禁止使用 AOM。"""
    return False


def use_prometheus(tenant: object, *, client: object | None = None) -> bool:
    if tenant is None or not str(getattr(tenant, "prometheus_url", None) or "").strip():
        return False
    m = mode(tenant)
    return m in ("PROMETHEUS", "AUTO")


def effective(tenant: object, *, client: object | None = None) -> str:
    """旧显式 AOM 返回 NONE，不擅自切换其他数据源；原生字段不受影响。"""
    if use_ces(tenant):
        return "CES"
    return "PROMETHEUS" if use_prometheus(tenant, client=client) else "NONE"


def mark(tenant: object, name: str, ok: bool, reason: str = "", **extra: object) -> None:
    """记录能力探测结果（只在结果变化或首次探测时写入，避免每轮巡检都更新租户记录）。"""
    if tenant is None or name == "aom":
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
        with ces_access(getattr(ctx, "client", None), True):
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


def guarded_aom(ctx: "RunContext", fn: "Callable[[], T]", empty: "T") -> "T":
    """旧调用兼容：不执行函数，不请求或读取 AOM 缓存。"""
    return empty
