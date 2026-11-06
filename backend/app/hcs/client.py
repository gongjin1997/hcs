import logging
import math
import random
import threading
import time
from typing import Any
from urllib.parse import unquote

import httpx

from app.core.config import get_settings

from .auth import AkSkAuth, Auth, TokenAuth
from .diagnostics import error_code, network_error, record_attempt, request_id, retry_after
from .endpoints import EndpointResolver
from .errors import HcsError
from .ratelimit import acquire_cloud, consume_budget, remaining_time, request_budget, service_limiter
from .readonly import ces_authorized, gaussdb_query_post
from .transport import send_bounded

log = logging.getLogger(__name__)


def metrics_delta(after: dict, before: dict) -> dict:
    """复用客户端时，运行记录仅记本轮调用与等待，不累计前一次任务。"""
    out = {key: max(0, after.get(key, 0) - before.get(key, 0)) for key in ("calls", "errors", "rate_limit_ms")}
    out["rate_limit_ms"] = round(out["rate_limit_ms"], 2)
    old_services = before.get("services", {})
    out["services"] = {service: {key: max(0, values.get(key, 0) - old_services.get(service, {}).get(key, 0))
                                 for key in ("calls", "errors", "rate_limit_ms")}
                       for service, values in after.get("services", {}).items()}
    return out


class HcsClient:
    """统一的 HCS OpenAPI 客户端：认证（Token / AK/SK 签名）、按服务限流、401 自动刷新 Token、429/5xx 指数退避重试、调用计数。

    path 中的 {project_id} 会被替换为当前项目 ID，例如 "/v2/{project_id}/instances"；
    host_vars 用于 Endpoint 模板中的变量，例如 CCE 集群 Kubernetes API 的 {cluster_id}。
    """

    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, http: httpx.Client, auth: Auth, resolver: EndpointResolver, project_id: str,
                 qps: dict[str, float] | None = None, max_retries: int | None = None, backoff: float = 0.5, tenant: str = "",
                 mock_url: str | None = None, tenant_id: int | None = None, project_ref: int | None = None,
                 max_concurrency: int | None = None, ces_enabled: bool = False):
        settings = get_settings()
        self.http, self.auth, self.resolver, self.project_id = http, auth, resolver, project_id
        self.mock_url = mock_url                   # 开发 / 演示：Mock HCS 地址（Prometheus 等外部数据源也转到 Mock）
        attempts = max_retries + 1 if max_retries is not None else getattr(settings, "hcs_max_attempts", 3)
        self.max_retries, self.backoff, self.tenant = min(4, max(1, attempts)) - 1, max(0, backoff), tenant
        self.tenant_id, self.project_ref = tenant_id, project_ref
        self.ces_enabled = ces_enabled is True
        self.max_concurrency = int(max_concurrency if max_concurrency is not None
                                   else getattr(settings, "hcs_service_concurrency", 2))
        self.acquire_timeout = getattr(settings, "hcs_acquire_timeout_seconds", 30)
        self._qps: dict[str, float] = {}
        self._configured_qps: set[str] = set()
        self.calls = 0
        self._stats_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._auth_generation = 0
        self._service_stats: dict[str, dict[str, float]] = {}
        self._last_summary_calls = 0
        self.configure_qps(qps or {}, _initial=True)

    def configure_qps(self, overrides: dict[str, float], *, _initial: bool = False) -> None:
        """更新本次任务的额度；共享限速器的时间与在途请求不会被重置。"""
        for service, rate in overrides.items():
            if not math.isfinite(rate) or rate <= 0:
                raise ValueError("QPS must be finite and positive")
            self._qps[service] = rate
            if not _initial:
                self._configured_qps.add(service)
        if isinstance(self.auth, TokenAuth):
            self.auth.provider.configure(self._qps.get("iam", 5), self.max_concurrency, self.acquire_timeout,
                                         self.tenant_id, self.project_ref, self._record,
                                         configured="iam" in self._configured_qps)

    def _record(self, service: str, *, calls: int = 0, errors: int = 0, wait_ms: float = 0) -> None:
        with self._stats_lock:
            stats = self._service_stats.setdefault(service, {"calls": 0, "errors": 0, "rate_limit_ms": 0})
            self.calls += calls
            stats["calls"] += calls
            stats["errors"] += errors
            stats["rate_limit_ms"] += wait_ms

    def metrics_snapshot(self) -> dict:
        with self._stats_lock:
            services = {key: dict(value) for key, value in self._service_stats.items()}
            snapshot = {"calls": self.calls, "errors": int(sum(v["errors"] for v in services.values())),
                        "rate_limit_ms": round(sum(v["rate_limit_ms"] for v in services.values()), 2), "services": services}
            changed = self.calls != self._last_summary_calls
            self._last_summary_calls = self.calls
        if changed:
            log.info("cloud.requests.summary", extra={"tenant_id": self.tenant_id, "project_ref": self.project_ref,
                     "upstream_calls": snapshot["calls"], "upstream_errors": snapshot["errors"],
                     "wait_ms": snapshot["rate_limit_ms"], "cloud_services": services})
        return snapshot

    def _send(self, service: str, method: str, path: str, *, params: dict | None = None, json: Any = None,
              headers: dict | None = None, host_vars: dict | None = None) -> httpx.Response:
        with request_budget(seconds=self.acquire_timeout):
            return self._send_scoped(service, method, path, params=params, json=json, headers=headers, host_vars=host_vars)

    def _send_scoped(self, service: str, method: str, path: str, *, params: dict | None = None, json: Any = None,
                     headers: dict | None = None, host_vars: dict | None = None) -> httpx.Response:
        service = service.strip().lower()
        if service == "aom":
            # 全局停用：旧配置、GET、已登记过的查询 POST 都不能触发解析、鉴权或网络请求。
            raise HcsError(0, "LOCAL.AOM_DISABLED", "当前版本已停用 AOM，未向上游发送请求")
        if service == "ces" and (not getattr(get_settings(), "hcs_ces_enabled", False)
                                 or not (self.ces_enabled or ces_authorized(self))):
            raise HcsError(0, "LOCAL.CES_DISABLED", "CES 未经部署及租户双重明确启用，未向上游发送请求")
        method = method.upper()
        header_names = {name.lower(): value for name, value in (headers or {}).items()}
        if {"x-http-method-override", "x-method-override", "x-http-method"}.intersection(header_names):
            raise HcsError(0, "LOCAL.READ_ONLY", "云查询不允许覆盖 HTTP 请求方法")
        allowed_post = (service == "ces" and path == "/V1.0/{project_id}/batch-query-metric-data" or
                        service == "gaussdb" and gaussdb_query_post(path))
        if method not in ("GET", "HEAD") and not (method == "POST" and allowed_post):
            raise HcsError(0, "LOCAL.READ_ONLY", "云客户端只允许已登记的查询接口")
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise HcsError(0, "LOCAL.INVALID_PATH", "云接口须使用逻辑路径，查询条件通过 params 传入")
        if any(segment in (".", "..") for segment in unquote(path).split("/")):
            raise HcsError(0, "LOCAL.INVALID_PATH", "云查询路径不允许跨越项目路径")
        if "{project_id}" in path and (not isinstance(self.project_id, str) or not self.project_id
                or any(c.isspace() or c in "/?#%\\{}" for c in self.project_id)):
            raise HcsError(0, "LOCAL.PROJECT_MISMATCH", "项目 ID 格式不正确，未向上游发送请求")
        if header_names.get("x-project-id", self.project_id) != self.project_id:
            raise HcsError(0, "LOCAL.PROJECT_MISMATCH", "请求头与客户端项目不一致，未向上游发送请求")
        auth_project = (self.auth.provider.project_id if isinstance(self.auth, TokenAuth)
                        else self.auth.project_id if isinstance(self.auth, AkSkAuth) else None)
        if auth_project is not None and auth_project != self.project_id:
            raise HcsError(0, "LOCAL.PROJECT_MISMATCH", "认证与客户端项目不一致，未向上游发送请求")
        try:
            url = self.resolver.url(service, path.replace("{project_id}", self.project_id), **(host_vars or {}))
        except (KeyError, ValueError):
            raise HcsError(0, "LOCAL.ENDPOINT", "云服务端点未配置完整") from None
        limiter = service_limiter(service)
        refreshed = False
        for attempt in range(1, self.max_retries + 2):
            consume_budget()
            hdrs = {"Content-Type": "application/json;charset=utf8", **(headers or {})}
            req = self.http.build_request(method, url, params=params, json=json, headers=hdrs)
            generation = self._auth_generation
            self.auth.apply(req, service)          # IAM 自己限速，不占业务服务的在途名额。
            waiting = time.perf_counter()
            try:
                lease = acquire_cloud(service, self._qps.get(service, 5), self.max_concurrency, self.acquire_timeout,
                                      configured=service in self._configured_qps)
            except HcsError as guard_error:
                wait_ms = (time.perf_counter() - waiting) * 1000
                self._record(service, errors=1, wait_ms=wait_ms)
                record_attempt(log, service=service, path=path, method=method, tenant_id=self.tenant_id,
                               project_ref=self.project_ref, status=0, attempt=attempt, cost_ms=0,
                               wait_ms=wait_ms, error=guard_error)
                raise
            wait_ms = (time.perf_counter() - waiting) * 1000
            self._record(service, wait_ms=wait_ms)
            started = time.perf_counter()
            resp: httpx.Response | None = None
            error: HcsError | None = None
            exception_type = None
            try:
                resp = send_bounded(self.http, req)
            except httpx.HTTPError as exc:
                error, exception_type = network_error(exc), type(exc).__name__
            except HcsError as exc:
                error, exception_type = exc, type(exc).__name__
            finally:
                lease.release()
                self._record(service, calls=1)
            cost_ms = (time.perf_counter() - started) * 1000
            status = resp.status_code if resp is not None else 0
            upstream_id = request_id(resp) if resp is not None else None
            if error is None and remaining_time(self.acquire_timeout) <= 0:
                error = HcsError(0, "LOCAL.REQUEST_BUDGET", "云查询时间预算已用尽，未使用超时结果")
            elif status >= 300:
                assert resp is not None
                message = ("上游认证失败（HTTP 401）：AK/SK 签名不匹配或凭据失效，请核对配置与系统时间"
                           if status == 401 and self.auth.kind == "AKSK" else f"上游查询返回 HTTP {status}")
                error = HcsError(status, error_code(resp, f"HTTP.{status}"), message, upstream_id)
            if error:
                self._record(service, errors=1)
            record_attempt(log, service=service, path=path, method=method, tenant_id=self.tenant_id,
                           project_ref=self.project_ref, status=status, attempt=attempt, cost_ms=cost_ms,
                           wait_ms=wait_ms, upstream_id=upstream_id, error=error, exception_type=exception_type)
            if error is None and resp is not None:
                return resp
            if status == 401 and isinstance(self.auth, TokenAuth):
                self.auth.reject(req.headers.get("X-Auth-Token"), req.extensions.get("inspector_token_generation"))
            if status == 401 and not refreshed and attempt <= self.max_retries:
                with self._refresh_lock:
                    if isinstance(self.auth, TokenAuth):
                        refreshed = self.auth.refresh_rejected(req.headers.get("X-Auth-Token"),
                                                               req.extensions.get("inspector_token_generation"))
                        if refreshed:
                            self._auth_generation += 1
                    elif generation != self._auth_generation:
                        refreshed = True
                    elif self.auth.refresh():
                        self._auth_generation += 1
                        refreshed = True
                if refreshed:
                    if resp is not None:
                        resp.close()
                    continue
            retry = (status in self.RETRY_STATUS or error is not None and error.code == "NETWORK"
                     and error.retryable is not False) \
                and attempt <= self.max_retries
            after = retry_after(resp.headers.get("Retry-After")) if resp is not None else None
            base = min(8, self.backoff * 2 ** (attempt - 1))
            delay = max(base, after or 0) + random.uniform(0, base / 4)
            if resp is not None:
                resp.close()
            if after is not None or retry:
                limiter.defer(delay)
            if not retry or delay > remaining_time(self.acquire_timeout):
                assert error is not None
                raise error from None
            time.sleep(delay)
        raise AssertionError("unreachable")

    def request(self, service: str, method: str, path: str, **kw: Any) -> dict:
        resp = self._send(service, method, path, **kw)
        try:
            body = resp.json() if resp.content else {}  # 部分接口（如 MRS V2 作业列表）成功时返回 202
        except ValueError:
            body = None
        if not isinstance(body, dict):
            error = HcsError(resp.status_code, "LOCAL.INVALID_RESPONSE", "上游查询未返回有效 JSON 对象", request_id(resp))
            self._record(service, errors=1)
            record_attempt(log, service=service, path=path, method=method, tenant_id=self.tenant_id,
                           project_ref=self.project_ref, status=resp.status_code, attempt=1,
                           cost_ms=0, wait_ms=0, upstream_id=request_id(resp), error=error)
            raise error
        return body

    def get(self, service: str, path: str, **kw: Any) -> dict:
        return self.request(service, "GET", path, **kw)

    def post(self, service: str, path: str, **kw: Any) -> dict:
        return self.request(service, "POST", path, **kw)

    def get_text(self, service: str, path: str, **kw: Any) -> str:
        """纯文本接口，例如经 kube-apiserver 代理抓取 Pod 的 /metrics（Prometheus 文本格式）。"""
        return self._send(service, "GET", path, **kw).text
