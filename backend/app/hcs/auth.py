"""HCS 认证方式：每个租户二选一。

- TokenAuth（auth_type=PASSWORD）：IAM 用户名 + 密码换取 Token，请求头 X-Auth-Token；Token 缓存并提前 1 小时刷新，
  收到 401 时强制刷新一次
- AkSkAuth（auth_type=AKSK）：每个请求用 AK/SK 签名（见 signer.py），无需 Token；项目级服务带 X-Project-Id，
  全局服务（IAM）带 X-Domain-Id
"""
import threading
import time
from datetime import datetime
from typing import Protocol

import httpx

from . import signer
from .errors import HcsError


class Auth(Protocol):
    kind: str

    def apply(self, req: httpx.Request, service: str) -> None: ...

    def refresh(self) -> bool: ...


class TokenProvider:
    """IAM Token 获取与缓存。

    POST {iam}/v3/auth/tokens，Token 在响应头 X-Subject-Token 中，有效期 24 小时；
    这里提前 1 小时刷新，并在收到 401 时由 HcsClient 调用 get(force=True) 强制刷新。
    """

    REFRESH_AHEAD = 3600

    def __init__(self, http: httpx.Client, iam_url: str, domain: str, username: str, password: str,
                 project_id: str):
        self.http, self.iam_url = http, iam_url.rstrip("/")
        self.domain, self.username, self._password, self.project_id = domain, username, password, project_id
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._expires_at - self.REFRESH_AHEAD:
                return self._token
            body = {"auth": {
                "identity": {"methods": ["password"], "password": {"user": {
                    "name": self.username, "password": self._password, "domain": {"name": self.domain}}}},
                "scope": {"project": {"id": self.project_id}},
            }}
            try:
                resp = self.http.post(f"{self.iam_url}/v3/auth/tokens", json=body)
            except httpx.TransportError as e:
                raise HcsError(0, "IAM.NETWORK", str(e)) from e
            if resp.status_code != 201 or "X-Subject-Token" not in resp.headers:
                raise HcsError(resp.status_code, "IAM.AUTH_FAILED", resp.text[:300])
            self._token = resp.headers["X-Subject-Token"]
            try:
                expires = resp.json()["token"]["expires_at"].replace("Z", "+00:00")
                self._expires_at = datetime.fromisoformat(expires).timestamp()
            except (KeyError, ValueError):
                self._expires_at = time.time() + 23 * 3600
            return self._token


class TokenAuth:
    kind = "PASSWORD"

    def __init__(self, provider: TokenProvider):
        self.provider = provider

    def apply(self, req: httpx.Request, service: str) -> None:
        req.headers["X-Auth-Token"] = self.provider.get()

    def refresh(self) -> bool:
        self.provider.get(force=True)
        return True

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
