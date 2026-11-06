"""云调用诊断只记录逻辑路由、状态和耗时，不读取上游响应体。"""
import errno
import logging
import re
import socket
import ssl
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite

import httpcore
import httpx

from app.core.logging import log_context

from .errors import HcsError

_STATIC = frozenset("api apis auth tokens batch-query-metric-data metrics query projects servers instances jobs status "
                    "clusters namespaces pods events proxy volumes vpcs subnets ports floatingips publicips bandwidths "
                    "security-groups security-group-rules loadbalancers listeners pools members flavors snapshots "
                    "workspaces nodes groups queues executions job-executions job-execution hosts networks "
                    "availability-zones availability_zones os-availability-zone os-server-groups detail config "
                    "databases configurations engines versions os-interface extensions dms dws css elb lbaas "
                    "endpoints services domains roles users regions tenants token project metric-data "
                    "cloudservers cluster_infos gaussdb".split())


def logical_path(path: str) -> str:
    path = path.split("?", 1)[0].split("#", 1)[0]
    return "/" + "/".join(
        part if part in _STATIC or re.fullmatch(r"[vV]\d+(?:\.\d+)*", part) or part == "{project_id}" else "{item}"
        for part in path.split("/") if part)[:512]


def request_id(response: httpx.Response) -> str | None:
    value = response.headers.get("X-Request-Id") or response.headers.get("X-Openstack-Request-Id")
    return value if value and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) else None


def error_code(response: httpx.Response, fallback: str) -> str:
    try:
        body = response.json()
    except ValueError:
        return fallback
    if not isinstance(body, dict):
        return fallback
    nested = body.get("error")
    code = body.get("error_code") or body.get("code") or (nested.get("code") if isinstance(nested, dict) else None)
    if isinstance(code, int) and not isinstance(code, bool):
        code = str(code)
    return code if isinstance(code, str) and len(code) <= 64 and re.fullmatch(
        r"(?:[A-Z][A-Z0-9_]*(?:[.-][A-Z0-9_]+)*|\d{3,8}|Forbidden|NotFound|AccessDenied|Unauthorized)", code) else fallback


def retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value) if value.strip().isdigit() else (
            parsedate_to_datetime(value).replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return None
    return max(0, seconds) if isfinite(seconds) and seconds <= 2**31 else None


_NETWORK_MESSAGES = {
    "certificate": "TLS 证书校验失败，请检查证书有效期、域名匹配及 CA 信任链",
    "dns": "getaddrinfo: 上游服务域名解析失败，请检查容器 DNS 和域名解析配置",
    "refused": "上游端口拒绝连接，请核对服务地址、端口和服务监听状态",
    "route": "上游网络不可达，请检查容器路由、防火墙及网络连通性",
    "denied": "网络访问被拒绝，请检查容器权限及防火墙访问策略",
    "reset": "上游连接被中断，请检查网络链路及上游服务状态",
    "connect_timeout": "连接上游服务超时，请检查网络连通性、防火墙和服务端口",
    "read_timeout": "读取上游响应超时，请检查上游服务负载及响应时间",
    "write_timeout": "发送上游请求超时，请检查网络和上游服务接收状态",
    "pool_timeout": "等待上游连接池超时，请检查并发请求数量及连接池配置",
    "timeout": "上游网络请求超时，请检查网络连通性及上游服务状态",
    "tls": "TLS 握手失败，请检查服务端 TLS 配置、协议版本及证书配置",
    "proxy": "上游代理连接失败，请检查代理配置、认证及代理可达性",
    "unknown": "上游网络请求失败，请检查容器网络、服务地址和端口",
}
_NETWORK_ERRNOS = {
    errno.ECONNREFUSED: "refused", 10061: "refused",
    errno.ENETUNREACH: "route", errno.EHOSTUNREACH: "route", errno.ENETDOWN: "route",
    10050: "route", 10051: "route", 10065: "route",
    errno.EACCES: "denied", errno.EPERM: "denied", 10013: "denied",
    errno.ECONNRESET: "reset", errno.ECONNABORTED: "reset", errno.EPIPE: "reset",
    10053: "reset", 10054: "reset",
}
_NETWORK_TEXT_HINTS = (
    ("certificate", ("certificate_verify_failed", "certificate verify failed", "certificate verification failed",
                     "hostname mismatch", "ip address mismatch", "certificate has expired", "self signed certificate",
                     "unable to get local issuer certificate")),
    ("dns", ("getaddrinfo", "name or service not known", "nodename nor servname", "no address associated",
             "temporary failure in name resolution", "name resolution failed")),
    ("refused", ("connection refused", "actively refused")),
    ("route", ("network is unreachable", "no route to host", "network is down")),
    ("reset", ("connection reset", "connection aborted", "broken pipe", "server disconnected")),
    ("tls", ("ssl:", "tls handshake", "ssl handshake", "wrong_version_number", "tlsv1_alert")),
    ("proxy", ("proxy error", "proxy authentication required", "proxy connect", "tunnel connection failed")),
    ("timeout", ("timed out", "timeout")),
)


def _network_causes(error: BaseException) -> list[BaseException]:
    # httpx/httpcore 的包装异常通常通过 cause/context 保留底层系统错误。
    # 两条链均有界遍历并防环；原异常及其文本只用于分类，绝不返回或记录。
    causes: list[BaseException] = []
    pending, seen = [error], set()
    while pending and len(causes) < 16:
        cause = pending.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        causes.append(cause)
        pending.extend(item for item in (cause.__context__, cause.__cause__) if item is not None)
    return causes


def _network_reason(error: BaseException) -> str:
    causes = _network_causes(error)
    if any(isinstance(cause, ssl.SSLCertVerificationError) for cause in causes):
        return "certificate"
    for cause in causes:
        if isinstance(cause, socket.gaierror):
            return "dns"
        if isinstance(cause, OSError) and not isinstance(cause, ssl.SSLError):
            # SSLError.errno 是 SSL 错误码，不能按 POSIX errno 解释。
            reason = _NETWORK_ERRNOS.get(cause.errno or 0) or _NETWORK_ERRNOS.get(getattr(cause, "winerror", 0) or 0)
            if reason:
                return reason
    for kind, reason in (("ConnectTimeout", "connect_timeout"), ("ReadTimeout", "read_timeout"),
                         ("WriteTimeout", "write_timeout"), ("PoolTimeout", "pool_timeout")):
        if any(isinstance(cause, (getattr(httpx, kind), getattr(httpcore, kind))) for cause in causes):
            return reason
    for types, reason in (((ssl.SSLError,), "tls"), ((httpx.ProxyError, httpcore.ProxyError), "proxy"),
                          ((TimeoutError, httpx.TimeoutException, httpcore.TimeoutException), "timeout")):
        if any(isinstance(cause, types) for cause in causes):
            return reason
    for cause in reversed(causes):
        try:
            text = str(cause)[:2048].casefold()
        except Exception:
            continue
        for reason, hints in _NETWORK_TEXT_HINTS:
            if any(hint in text for hint in hints):
                return reason
    return "unknown"


def network_error(error: httpx.HTTPError, *, iam: bool = False) -> HcsError:
    reason = _network_reason(error)
    permanent_dns = reason == "dns" and any(
        isinstance(cause, socket.gaierror) and cause.errno in
        (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME))
        for cause in _network_causes(error))
    # 证书/权限/域名不存在须先修配置；临时 DNS 故障和其他网络中断仍按原预算重试。
    return HcsError(0, "IAM.NETWORK" if iam else "NETWORK", _NETWORK_MESSAGES[reason], reason=reason,
                    retryable=reason not in ("certificate", "denied") and not permanent_dns)


def record_attempt(logger: logging.Logger, *, service: str, path: str, method: str, tenant_id: int | None,
                   project_ref: int | None, status: int, attempt: int, cost_ms: float, wait_ms: float,
                   upstream_id: str | None = None, error: HcsError | None = None,
                   exception_type: str | None = None) -> None:
    fields = {"cloud_service": service, "logical_path": logical_path(path), "cloud_method": method,
              "tenant_id": tenant_id, "project_ref": project_ref, "status": status, "attempt": attempt,
              "cost_ms": round(cost_ms, 2), "wait_ms": round(wait_ms, 2), "upstream_request_id": upstream_id}
    if exception_type:
        fields["exception_type"] = exception_type
    if error is not None and error.reason:
        fields.update(network_reason=error.reason, retryable=error.retryable)
    with log_context(**fields):
        logger.log(logging.WARNING if error else logging.DEBUG, "cloud.request.failed" if error else "cloud.request.ok",
                   extra=fields, exc_info=(type(error), error, None) if error else None)
