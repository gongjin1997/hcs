import logging
import time
from typing import Any

import httpx

from .auth import Auth
from .endpoints import EndpointResolver
from .errors import HcsError
from .ratelimit import TokenBucket

log = logging.getLogger(__name__)


class HcsClient:
    """统一的 HCS OpenAPI 客户端：认证（Token / AK/SK 签名）、按服务限流、401 自动刷新 Token、429/5xx 指数退避重试、调用计数。

    path 中的 {project_id} 会被替换为当前项目 ID，例如 "/v2/{project_id}/instances"；
    host_vars 用于 Endpoint 模板中的变量，例如 CCE 集群 Kubernetes API 的 {cluster_id}。
    """

    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, http: httpx.Client, auth: Auth, resolver: EndpointResolver, project_id: str,
                 qps: dict[str, float] | None = None, max_retries: int = 3, backoff: float = 0.5, tenant: str = "",
                 mock_url: str | None = None):
        self.http, self.auth, self.resolver, self.project_id = http, auth, resolver, project_id
        self.mock_url = mock_url                   # 开发 / 演示：Mock HCS 地址（Prometheus 等外部数据源也转到 Mock）
        self.max_retries, self.backoff, self.tenant = max_retries, backoff, tenant
        self._buckets = {svc: TokenBucket(rate) for svc, rate in (qps or {}).items()}
        self._default_bucket = TokenBucket(10)
        self.calls = 0

    def _send(self, service: str, method: str, path: str, *, params: dict | None = None, json: Any = None,
              headers: dict | None = None, host_vars: dict | None = None) -> httpx.Response:
        url = self.resolver.url(service, path.replace("{project_id}", self.project_id), **(host_vars or {}))
        bucket = self._buckets.get(service, self._default_bucket)
        refreshed, attempt = False, 0
        while True:
            if not bucket.acquire():
                raise HcsError(0, "LOCAL.RATE_LIMIT", f"{service} 限流等待超时")
            hdrs = {"Content-Type": "application/json;charset=utf8", **(headers or {})}
            req = self.http.build_request(method, url, params=params, json=json, headers=hdrs)
            self.auth.apply(req, service)          # 每次重试重新签名（X-Sdk-Date 随之更新）
            started = time.perf_counter()
            try:
                resp = self.http.send(req)
            except httpx.TransportError as e:
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(self.backoff * 2 ** (attempt - 1))
                    continue
                raise HcsError(0, "NETWORK", f"{method} {url}: {e}") from e
            finally:
                self.calls += 1
            log.debug("hcs %s %s -> %s (%.0f ms)", method, url, resp.status_code,
                      (time.perf_counter() - started) * 1000)
            if resp.status_code == 401 and not refreshed and self.auth.refresh():
                refreshed = True
                continue
            if resp.status_code in self.RETRY_STATUS and attempt < self.max_retries:
                attempt += 1
                time.sleep(self.backoff * 2 ** (attempt - 1))
                continue
            if resp.status_code >= 400:
                code, msg = "HTTP", resp.text[:300]
                try:
                    body = resp.json()
                    code = body.get("error_code") or body.get("code") or code
                    msg = body.get("error_msg") or body.get("message") or msg
                except ValueError:
                    pass
                raise HcsError(resp.status_code, str(code), str(msg), resp.headers.get("X-Request-Id"))
            return resp

    def request(self, service: str, method: str, path: str, **kw: Any) -> dict:
        resp = self._send(service, method, path, **kw)
        return resp.json() if resp.content else {}      # 部分接口（如 MRS V2 作业列表）成功时返回 202

    def get(self, service: str, path: str, **kw: Any) -> dict:
        return self.request(service, "GET", path, **kw)

    def post(self, service: str, path: str, **kw: Any) -> dict:
        return self.request(service, "POST", path, **kw)

    def get_text(self, service: str, path: str, **kw: Any) -> str:
        """纯文本接口，例如经 kube-apiserver 代理抓取 Pod 的 /metrics（Prometheus 文本格式）。"""
        return self._send(service, "GET", path, **kw).text
