"""Prometheus 指标来源（可选）：CES 不可用时，用租户侧 Prometheus（node_exporter）补齐 ECS 的使用率与开机时间。

按 node_exporter 的 instance 标签（"10.10.1.11:9100"）中的 IP 关联 ECS 私网 IP；查询语句可按局点调整（任务参数 prometheus）。
"""
from typing import Any

import httpx

from app.collectors.base import RunContext

FS = 'fstype!~"tmpfs|overlay|squashfs|iso9660"'
DEFAULT_QUERIES = {
    "cpu_util": '100 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100',
    "mem_util": "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100",
    "disk_util": f"(1 - node_filesystem_avail_bytes{{{FS}}} / node_filesystem_size_bytes{{{FS}}}) * 100",
    "boot_time": "node_boot_time_seconds",
}


class PromError(Exception):
    pass


class PromClient:
    def __init__(self, base: str, http: httpx.Client | None = None, timeout: float = 10.0):
        self.base, self.timeout = base.rstrip("/"), timeout
        self.http = http or httpx.Client(timeout=timeout)
        self.calls = 0

    def query(self, promql: str) -> list[dict]:
        self.calls += 1
        try:
            resp = self.http.get(f"{self.base}/api/v1/query", params={"query": promql}, timeout=self.timeout)
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise PromError(f"Prometheus 请求失败：{e}") from e
        if resp.status_code >= 400 or body.get("status") != "success":
            raise PromError(f"Prometheus 返回错误：{body.get('error') or resp.status_code}")
        return (body.get("data") or {}).get("result") or []

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
    return PromClient(base, http=getattr(ctx.client, "http", None))


def host_of(labels: dict[str, Any], label: str = "instance") -> str:
    v = str(labels.get(label) or "")
    if v.startswith("["):                       # [IPv6]:port
        return v[1:v.find("]")]
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def ecs_metrics(prom: PromClient, ip_index: dict[str, str], queries: dict[str, str] | None = None,
                label: str = "instance") -> dict[str, dict]:
    """返回 {ECS ID: {cpu_util, mem_util, disk_util, disk_mount, boot_time}}；查不到的实例不返回。"""
    q = {**DEFAULT_QUERIES, **(queries or {})}
    out: dict[str, dict] = {}
    for key, promql in q.items():
        for series in prom.query(promql):
            sid = ip_index.get(host_of(series.get("metric") or {}, label))
            if sid is None:
                continue
            try:
                value = float((series.get("value") or [0, "nan"])[1])
            except (TypeError, ValueError):
                continue
            if value != value:      # NaN
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
