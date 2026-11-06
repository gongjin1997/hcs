"""Prometheus 指标来源（可选）：CES 不可用时，用租户侧 Prometheus（node_exporter）补齐 ECS 的使用率与开机时间。

按 node_exporter 的 instance 标签（"10.10.1.11:9100"）中的 IP 关联 ECS 私网 IP；查询语句可按局点调整（任务参数 prometheus）。
"""
import logging
import math
import time
from collections.abc import Callable
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.collectors.base import RunContext
from app.hcs import HcsClient
from app.hcs.diagnostics import network_error, record_attempt, request_id, retry_after
from app.hcs.errors import HcsError
from app.hcs.ratelimit import acquire_cloud, consume_budget, remaining_time, request_budget, service_limiter
from app.hcs.transport import send_bounded

log = logging.getLogger(__name__)

FS = 'fstype!~"tmpfs|overlay|squashfs|iso9660"'
DEFAULT_QUERIES = {
    "cpu_util": '100 - avg without (cpu, mode) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100',
    "mem_util": "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100",
    "disk_util": f"(1 - node_filesystem_avail_bytes{{{FS}}} / node_filesystem_size_bytes{{{FS}}}) * 100",
    "boot_time": "node_boot_time_seconds",
}


class PromError(Exception):
    pass


class PromClient:
    def __init__(self, base: str, http: httpx.Client | None = None, timeout: float = 10.0, *,
                 tenant_id: int | None = None, project_ref: int | None = None,
                 observer: Callable[..., None] | None = None):
        self.base, self.timeout = base.rstrip("/"), timeout
        self.http = http or httpx.Client(timeout=timeout)
        self.calls = 0
        self.evaluation_time: float | None = None
        self.tenant_id, self.project_ref, self.observer = tenant_id, project_ref, observer

    def query(self, promql: str) -> list[dict]:
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise PromError("Prometheus 查询超时预算必须为有限正数")
        try:
            with request_budget(seconds=self.timeout):
                return self._query(promql)
        except HcsError as error:
            raise PromError(error.message) from None

    def _query(self, promql: str) -> list[dict]:
        limiter, waiting = service_limiter("prometheus"), time.perf_counter()
        consume_budget()
        try:
            lease = acquire_cloud("prometheus", 5, 2, self.timeout)
        except HcsError as guard_error:
            if self.observer:
                self.observer("prometheus", errors=1, wait_ms=(time.perf_counter() - waiting) * 1000)
            record_attempt(log, service="prometheus", path="/api/v1/query", method="GET", tenant_id=self.tenant_id,
                           project_ref=self.project_ref, status=0, attempt=1, cost_ms=0,
                           wait_ms=(time.perf_counter() - waiting) * 1000, error=guard_error)
            raise PromError(guard_error.message) from None
        wait_ms = (time.perf_counter() - waiting) * 1000
        self.calls += 1
        started = time.perf_counter()
        resp: httpx.Response | None = None
        error: HcsError | None = None
        exception_type = None
        data = None
        try:
            params: dict = {"query": promql}
            if self.evaluation_time is not None:
                params["time"] = self.evaluation_time
            request = self.http.build_request("GET", f"{self.base}/api/v1/query", params=params)
            resp = send_bounded(self.http, request, timeout=self.timeout)
            if remaining_time(self.timeout) <= 0:
                error = HcsError(0, "LOCAL.REQUEST_BUDGET", "Prometheus 查询时间预算已用尽，未使用超时结果")
            elif resp.status_code >= 300:
                error = HcsError(resp.status_code, "PROM.HTTP", f"Prometheus 返回 HTTP {resp.status_code}")
            else:
                body = resp.json()
                data = body.get("data") if isinstance(body, dict) and body.get("status") == "success" else None
                if (not isinstance(data, dict) or data.get("resultType") != "vector"
                        or not isinstance(data.get("result"), list) or body.get("warnings")):
                    error = HcsError(resp.status_code, "PROM.INVALID_RESPONSE", "Prometheus 返回无效查询结果")
        except httpx.HTTPError as exc:
            error, exception_type = network_error(exc), type(exc).__name__
        except ValueError:
            error = HcsError(0, "PROM.INVALID_RESPONSE", "Prometheus 返回无效查询结果")
        except HcsError as exc:
            error, exception_type = exc, type(exc).__name__
        finally:
            lease.release()
        status = resp.status_code if resp is not None else 0
        if self.observer:
            self.observer("prometheus", calls=1, errors=int(error is not None), wait_ms=wait_ms)
        record_attempt(log, service="prometheus", path="/api/v1/query", method="GET", tenant_id=self.tenant_id,
                       project_ref=self.project_ref, status=status, attempt=1,
                       cost_ms=(time.perf_counter() - started) * 1000, wait_ms=wait_ms,
                       upstream_id=request_id(resp) if resp is not None else None,
                       error=error, exception_type=exception_type)
        if error:
            after = retry_after(resp.headers.get("Retry-After")) if resp is not None else None
            if after is not None:
                try:
                    limiter.defer(after)
                except HcsError:
                    pass  # 不重试；Redis 故障时后续 acquire 同样拒绝放行。
            raise PromError(error.message) from None
        assert isinstance(data, dict)
        return data["result"]

    def ping(self) -> int:
        """连通性测试：返回 up 序列数（抓取目标数）。"""
        return len(self.query("up"))


def prom_for(ctx: RunContext) -> PromClient | None:
    tenant = ctx.tenant
    url = getattr(tenant, "prometheus_url", None)
    if not url:
        return None
    mock = getattr(ctx.client, "mock_url", None)
    base = f"{mock}/prom/{ctx.account}" if mock else url       # 演示环境：Prometheus 也由 Mock 模拟
    return PromClient(base, http=getattr(ctx.client, "http", None), tenant_id=ctx.tenant_id, project_ref=ctx.project_ref,
                      observer=ctx.client._record if isinstance(ctx.client, HcsClient) else None)


def host_of(labels: dict[str, Any], label: str = "instance") -> str:
    value = labels.get(label)
    if not isinstance(value, str) or not value or value.strip() != value:
        return ""
    try:
        ip_address(value)  # 不把未加方括号的 IPv6 末段误当端口。
        return value
    except ValueError:
        pass
    try:
        parsed = urlsplit(f"//{value}")
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            return ""
        _ = parsed.port
        return parsed.hostname
    except ValueError:
        return ""


def ecs_metrics(prom: PromClient, ip_index: dict[str, str], queries: dict[str, str] | None = None,
                label: str = "instance") -> dict[str, dict]:
    """返回 {ECS ID: {cpu_util, mem_util, disk_util, disk_mount, boot_time}}；查不到的实例不返回。"""
    q = {**DEFAULT_QUERIES, **(queries or {})}
    out: dict[str, dict] = {}
    for key, promql in q.items():
        for series in prom.query(promql):
            if not isinstance(series, dict) or not isinstance(series.get("metric"), dict):
                continue
            sid = ip_index.get(host_of(series.get("metric") or {}, label))
            if sid is None:
                continue
            try:
                sample = series.get("value")
                if not isinstance(sample, (list, tuple)) or len(sample) < 2 or isinstance(sample[1], bool):
                    continue
                value = float(sample[1])
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(value):
                continue
            cur = out.setdefault(sid, {})
            if key == "disk_util":
                if value > cur.get("disk_util", -1):
                    cur["disk_util"], cur["disk_mount"] = round(value, 1), (series.get("metric") or {}).get(
                        "mountpoint", "-")
            elif key == "boot_time":
                cur["boot_time"] = int(value)
            else:
                cur[key] = round(value, 1)
    return out
