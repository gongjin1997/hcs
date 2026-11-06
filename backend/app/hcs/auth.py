"""HCS 认证方式：每个租户二选一。

- TokenAuth（auth_type=PASSWORD）：IAM 用户名 + 密码换取 Token，请求头 X-Auth-Token；按实际有效期缓存并自适应提前刷新，
  收到 401 时强制刷新一次
- AkSkAuth（auth_type=AKSK）：每个请求用 AK/SK 签名（见 signer.py），无需 Token；项目级服务带 X-Project-Id，
  全局服务（IAM）带 X-Domain-Id
"""
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Protocol

import httpx

from app.core.logging import log_context

from . import signer
from .diagnostics import error_code, network_error, record_attempt, request_id, retry_after
from .endpoints import EndpointResolver
from .errors import HcsError
from .ratelimit import acquire_cloud, consume_budget, remaining_time, request_budget, service_limiter
from .token_cache import TokenCache, TokenRecord, TokenState, from_response
from .transport import send_bounded

log = logging.getLogger(__name__)


_tokens: OrderedDict[str, TokenState] = OrderedDict()
_tokens_lock = threading.Lock()


class Auth(Protocol):
    kind: str

    def apply(self, req: httpx.Request, service: str) -> None: ...

    def refresh(self) -> bool: ...


class TokenProvider:
    """IAM Token 获取与缓存。

    Token 的有效期严格取响应 expires_at，不假设 HCS 局点固定寿命。
    提前量取取得时剩余寿命的 10%，最多一小时；短 TTL 不会每个接口重新换发。
    """

    def __init__(self, http: httpx.Client, iam_url: str, domain: str, username: str, password: str,
                 project_id: str, domain_id: str | None = None, *, tenant_id: int | None = None,
                 project_ref: int | None = None, endpoint_scope: str = ""):
        self.http, self.iam_url = http, iam_url.rstrip("/")
        self.domain, self.username, self._password, self.project_id = domain, username, password, project_id
        self.domain_id = domain_id
        self.endpoint_scope = endpoint_scope
        self._token: str | None = None
        self._expires_at = 0.0
        self._generation: str | None = None
        self.configure(5, 2, 30, tenant_id, project_ref, lambda *_a, **_kw: None)

    def configure(self, qps: float, concurrency: int, timeout: float, tenant_id: int | None,
                  project_ref: int | None, observer: Callable[..., None], *, configured: bool = False) -> None:
        self.qps, self.concurrency, self.timeout = qps, concurrency, timeout
        self.tenant_id, self.project_ref, self.observer = tenant_id, project_ref, observer
        self.configured_qps = configured

    def _scope(self) -> str:
        identity = [self.tenant_id, self.project_ref, self.iam_url, self.domain, self.domain_id,
                    self.username, self._password, self.project_id, self.endpoint_scope]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()

    def cache_identity(self) -> str:
        """仅供内部查询缓存隔离；不含 Token，也不应写入日志或接口。"""
        return self._scope()

    def _state(self, key: str, *, pin: bool = False) -> TokenState:
        # 认证配置摘要仅作隔离键，不落日志；Redis 的 Token 内容必须经 MASTER_KEY 加密。
        with _tokens_lock:
            if key not in _tokens:
                if len(_tokens) >= 256:
                    victim = next((k for k, v in _tokens.items() if not v.lock.locked() and v.users == 0), None)
                    if victim is None:
                        raise HcsError(0, "LOCAL.IAM_BUSY", "IAM 认证请求繁忙，请稍后重试")
                    del _tokens[victim]
                _tokens[key] = TokenState()
            _tokens.move_to_end(key)
            if pin:
                _tokens[key].users += 1
            return _tokens[key]

    def get(self, force: bool = False, *, refresh: bool = False, rejected_token: str | None = None,
            rejected_generation: str | None = None) -> str:
        return self.get_record(force, refresh=refresh, rejected_token=rejected_token,
                               rejected_generation=rejected_generation).token

    def get_record(self, force: bool = False, *, refresh: bool = False, rejected_token: str | None = None,
                   rejected_generation: str | None = None) -> TokenRecord:
        if refresh and rejected_token is None and rejected_generation is None:
            rejected_token, rejected_generation = self._token, self._generation
        scope = self._scope()
        with self._locked(scope) as state, log_context(cloud_service="iam", tenant_id=self.tenant_id,
                                                       project_ref=self.project_ref):
            record = TokenCache().get(scope, state, self._load_token, force=force, refresh=refresh,
                                      rejected_token=rejected_token, rejected_generation=rejected_generation,
                                      timeout=self.timeout)
            self._token, self._expires_at, self._generation = record.token, record.expires_at, record.generation
            return record

    @contextmanager
    def _locked(self, scope: str) -> Iterator[TokenState]:
        state = self._state(scope, pin=True)
        locked = False
        try:
            locked = state.lock.acquire(timeout=remaining_time(self.timeout + 15))
            if not locked:
                raise HcsError(0, "IAM.CACHE_BUSY", "IAM 换发正在处理，请稍后重试")
            yield state
        finally:
            if locked:
                state.lock.release()
            with _tokens_lock:
                state.users -= 1

    def reject(self, token: str | None, generation: str | None) -> None:
        scope = self._scope()
        with self._locked(scope) as state:
            TokenCache.reject_local(state, token, generation)
        with log_context(cloud_service="iam", tenant_id=self.tenant_id, project_ref=self.project_ref):
            TokenCache.invalidate_shared(scope, generation)

    def _load_token(self) -> TokenRecord:
        user: dict = {"name": self.username, "password": self._password}
        if self.domain_id:
            user["domain"] = {"id": self.domain_id}
        elif self.domain:
            user["domain"] = {"name": self.domain}
        body = {"auth": {"identity": {"methods": ["password"], "password": {"user": user}},
                         "scope": {"project": {"id": self.project_id}}}}
        response = self._request(body)
        try:
            try:
                body = response.json()
            except ValueError:
                body = {}
            token = body.get("token") if isinstance(body, dict) else None
            project = token.get("project") if isinstance(token, dict) else None
            if isinstance(project, dict) and project.get("id") not in (None, "", self.project_id):
                raise HcsError(0, "LOCAL.PROJECT_MISMATCH", "IAM 返回的项目作用域不一致，未使用该 Token")
            return from_response(body, response.headers.get("X-Subject-Token"), time.time())
        except HcsError as error:
            self.observer("iam", errors=1)
            record_attempt(log, service="iam", path="/v3/auth/tokens", method="POST", tenant_id=self.tenant_id,
                           project_ref=self.project_ref, status=response.status_code, attempt=1, cost_ms=0, wait_ms=0,
                           upstream_id=request_id(response), error=error)
            raise

    def _request(self, body: dict) -> httpx.Response:
        with request_budget(seconds=self.timeout):
            return self._request_scoped(body)

    def _request_scoped(self, body: dict) -> httpx.Response:
        try:
            url = EndpointResolver({"iam": self.iam_url}, "", "").url("iam", "/v3/auth/tokens")
        except (KeyError, ValueError):
            raise HcsError(0, "LOCAL.ENDPOINT", "IAM 端点格式不正确，未向上游发送认证信息") from None
        consume_budget()
        limiter = service_limiter("iam")
        waiting = time.perf_counter()
        try:
            lease = acquire_cloud("iam", self.qps, self.concurrency, self.timeout, configured=self.configured_qps)
        except HcsError as guard_error:
            wait_ms = (time.perf_counter() - waiting) * 1000
            self.observer("iam", errors=1, wait_ms=wait_ms)
            record_attempt(log, service="iam", path="/v3/auth/tokens", method="POST", tenant_id=self.tenant_id,
                           project_ref=self.project_ref, status=0, attempt=1, cost_ms=0, wait_ms=wait_ms, error=guard_error)
            raise
        wait_ms = (time.perf_counter() - waiting) * 1000
        started = time.perf_counter()
        response: httpx.Response | None = None
        error: HcsError | None = None
        exception_type = None
        try:
            request = self.http.build_request("POST", url, json=body)
            response = send_bounded(self.http, request)
        except httpx.HTTPError as exc:
            error, exception_type = network_error(exc, iam=True), type(exc).__name__
        except HcsError as exc:
            error, exception_type = exc, type(exc).__name__
        finally:
            lease.release()
        status = response.status_code if response is not None else 0
        if error is None and remaining_time(self.timeout) <= 0:
            error = HcsError(0, "LOCAL.REQUEST_BUDGET", "IAM 认证时间预算已用尽，未使用超时结果")
        elif response is not None and (status != 201 or not response.headers.get("X-Subject-Token")):
            error = HcsError(status, error_code(response, "IAM.AUTH_FAILED"), f"IAM 认证失败（HTTP {status}）",
                             request_id(response))
        self.observer("iam", calls=1, errors=int(error is not None), wait_ms=wait_ms)
        record_attempt(log, service="iam", path="/v3/auth/tokens", method="POST", tenant_id=self.tenant_id,
                       project_ref=self.project_ref, status=status, attempt=1,
                       cost_ms=(time.perf_counter() - started) * 1000, wait_ms=wait_ms,
                       upstream_id=request_id(response) if response is not None else None,
                       error=error, exception_type=exception_type)
        if error:
            if response is not None:
                after = retry_after(response.headers.get("Retry-After"))
                response.close()
                if after is not None:
                    limiter.defer(after)
            raise error from None
        assert response is not None
        return response


class TokenAuth:
    kind = "PASSWORD"

    def __init__(self, provider: TokenProvider):
        self.provider = provider

    def apply(self, req: httpx.Request, service: str) -> None:
        record = self.provider.get_record()
        req.headers["X-Auth-Token"] = record.token
        req.extensions["inspector_token_generation"] = record.generation

    def refresh(self) -> bool:
        self.provider.get(force=True, refresh=True)
        return True

    def refresh_rejected(self, token: str | None, generation: str | None) -> bool:
        self.provider.get(force=True, refresh=True, rejected_token=token, rejected_generation=generation)
        return True

    def reject(self, token: str | None, generation: str | None) -> None:
        self.provider.reject(token, generation)

    def check(self) -> None:
        """连通性测试：强制换一次 Token。"""
        self.provider.get(force=True)


class AkSkAuth:
    kind = "AKSK"

    def __init__(self, access_key: str, secret_key: str, project_id: str | None = None, domain_id: str | None = None):
        if not access_key or not secret_key:
            raise HcsError(0, "LOCAL.AKSK_MISSING", "租户未配置 AK/SK")
        self.access_key, self._secret_key = access_key, secret_key
        self.project_id, self.domain_id = project_id, domain_id

    def apply(self, req: httpx.Request, service: str) -> None:
        if service == "iam":
            if self.domain_id:
                req.headers["X-Domain-Id"] = self.domain_id
        elif self.project_id:
            req.headers["X-Project-Id"] = self.project_id
        # 只对已确定的头签名（httpx 发送时追加的 user-agent / accept 等不参与签名）
        to_sign = {k: v for k, v in req.headers.items()
                   if k.lower() in ("host", "content-type", "x-project-id", "x-domain-id", "workspace")}
        for k, v in signer.sign(req.method, str(req.url), to_sign, req.content, self.access_key,
                                self._secret_key).items():
            req.headers[k] = v

    def refresh(self) -> bool:
        return False             # 签名失败（401）重试也无意义：AK/SK 错误或已停用
