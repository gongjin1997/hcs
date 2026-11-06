"""云信 HTTP3.0 群发传输：只报告网关受理，不重试、不推断终端送达。

端点必须完整配置。自建客户端禁用环境代理和重定向；注入客户端由调用方负责
TLS/代理/底层重试配置及关闭，本模块仍强制单次请求、禁止重定向并设置超时。
"""

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

MAX_BATCH = 500
MAX_EXPAND_PORT = 14
_CODES = frozenset({0, 2, 4, 6, 7, 9, 10, 11, 30, 31, 32, 34, 41, 42, 44, 45, 46, 47, 48, 99})
_ERRORS = {
    "INVALID_REQUEST": "短信网关请求参数无效",
    "CONNECT": "短信网关连接失败",
    "TIMEOUT": "短信网关请求超时，受理状态未确认",
    "TRANSPORT": "短信网关通信异常，受理状态未确认",
    "AUTH": "短信网关认证失败",
    "HTTP_ERROR": "短信网关 HTTP 异常，受理状态未确认",
    "INVALID_RESPONSE": "短信网关响应无效，受理状态未确认",
}


class SmsGatewayError(Exception):
    """仅暴露安全原因码与不确定标志，不保留请求、响应、地址或原始异常。"""

    def __init__(self, reason_code: str, *, uncertain: bool):
        self.reason_code = reason_code
        self.uncertain = uncertain
        super().__init__(_ERRORS.get(reason_code, "短信网关调用失败"))


@dataclass(frozen=True)
class SmsRecipientResult:
    phone: str = field(repr=False)
    accepted: bool
    code: str
    message_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class SmsBatchResult:
    results: list[SmsRecipientResult]
    code: str = "0"


def _invalid_request() -> None:
    raise SmsGatewayError("INVALID_REQUEST", uncertain=False)


def _validate(
    endpoint: str,
    username: str,
    password: str,
    content: str,
    phones: list[str],
    batch_id: str,
    expand_port: str | None,
    verify_tls: bool,
    timeout_seconds: float,
    timestamp_ms: int,
) -> None:
    if (
        not isinstance(endpoint, str)
        or not endpoint
        or any(c.isspace() for c in endpoint)
        or not all(isinstance(v, str) and v.strip() for v in (username, password, content, batch_id))
    ):
        _invalid_request()
    url: httpx.URL | None = None
    try:
        url = httpx.URL(endpoint)
    except (httpx.InvalidURL, ValueError):
        pass
    if url is None or url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.fragment:
        _invalid_request()
    if (
        not isinstance(phones, list)
        or not 1 <= len(phones) <= MAX_BATCH
        or any(not isinstance(phone, str) or not phone.strip() for phone in phones)
    ):
        _invalid_request()
    if len(set(phones)) != len(phones):
        _invalid_request()
    if expand_port is not None and (not isinstance(expand_port, str) or len(expand_port) > MAX_EXPAND_PORT):
        _invalid_request()
    if (
        type(verify_tls) is not bool
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or type(timestamp_ms) is not int
        or not 0 <= timestamp_ms <= 2**63 - 1
    ):
        _invalid_request()


def _request_body(
    username: str, password: str, content: str, phones: list[str], batch_id: str, expand_port: str | None, timestamp_ms: int
) -> bytes:
    encoded: bytes | None = None
    try:
        # 协议指定 MD5；它仅用于网关签名，不用于本系统密码存储。
        password_md5 = hashlib.md5(password.encode("utf-8"), usedforsecurity=False).hexdigest()
        sign = hashlib.md5(f"{username}{timestamp_ms}{password_md5}".encode(), usedforsecurity=False).hexdigest()
        rows = [{"toPhone": phone, **({"expandPort": expand_port} if expand_port else {})} for phone in phones]
        payload = {
            "userName": username,
            "timestamp": timestamp_ms,
            "sign": sign,
            "content": content,
            "smsBatchId": batch_id,
            "body": rows,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (UnicodeError, ValueError):
        pass
    if encoded is None:
        _invalid_request()
    assert encoded is not None
    return encoded


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _invalid_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


def _code(value: Any) -> str:
    if type(value) is int and value in _CODES:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,2}", value) and int(value) in _CODES:
        return str(int(value))
    raise SmsGatewayError("INVALID_RESPONSE", uncertain=True)


def _parse(response: httpx.Response, phones: list[str]) -> SmsBatchResult:
    if response.status_code in {401, 403}:
        raise SmsGatewayError("AUTH", uncertain=False)
    if not 200 <= response.status_code < 300:
        raise SmsGatewayError("HTTP_ERROR", uncertain=True)
    payload: Any = None
    try:
        payload = json.loads(
            response.content.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except (UnicodeError, ValueError):
        pass
    if not isinstance(payload, dict):
        raise SmsGatewayError("INVALID_RESPONSE", uncertain=True)
    root_code = _code(payload.get("code"))
    if root_code != "0":
        return SmsBatchResult([SmsRecipientResult(phone, False, root_code) for phone in phones], code=root_code)
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != len(phones):
        raise SmsGatewayError("INVALID_RESPONSE", uncertain=True)
    wanted = set(phones)
    results: dict[str, SmsRecipientResult] = {}
    for item in data:
        if not isinstance(item, dict):
            raise SmsGatewayError("INVALID_RESPONSE", uncertain=True)
        phone = item.get("toPhone")
        if not isinstance(phone, str) or phone not in wanted or phone in results:
            raise SmsGatewayError("INVALID_RESPONSE", uncertain=True)
        code = _code(item.get("code"))
        message_id = item.get("msgId")
        if message_id is not None and not (type(message_id) is int or isinstance(message_id, str) and message_id.strip()):
            raise SmsGatewayError("INVALID_RESPONSE", uncertain=True)
        results[phone] = SmsRecipientResult(phone, code == "0", code, str(message_id) if message_id is not None else None)
    return SmsBatchResult([results[phone] for phone in phones], code=root_code)


def send_batch(
    endpoint: str,
    username: str,
    password: str,
    content: str,
    phones: list[str],
    *,
    batch_id: str,
    expand_port: str | None = None,
    verify_tls: bool = True,
    timeout_seconds: float = 5,
    http: httpx.Client | None = None,
    timestamp_ms: int | None = None,
) -> SmsBatchResult:
    """提交一次群发；异常 uncertain=True 时调用方不得据此自动重发。

    本函数不检查业务总开关、租户开关、人员目录或模板审批，由调用方在入队及投递前检查。
    注入 http 时不关闭、不修改客户端；verify_tls 仅配置本函数自己创建的客户端。
    """
    stamp = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    _validate(endpoint, username, password, content, phones, batch_id, expand_port, verify_tls, timeout_seconds, stamp)
    body = _request_body(username, password, content, phones, batch_id, expand_port, stamp)
    options: dict[str, Any] = {
        "content": body,
        "headers": {"Content-Type": "application/json;charset=utf-8", "Accept": "application/json"},
        "timeout": timeout_seconds,
        "follow_redirects": False,
    }
    response: httpx.Response | None = None
    failure: SmsGatewayError | None = None
    try:
        if http is None:
            with httpx.Client(verify=verify_tls, timeout=timeout_seconds, follow_redirects=False, trust_env=False) as client:
                response = client.post(endpoint, **options)
        else:
            response = http.post(endpoint, **options)
    except httpx.ConnectError:
        failure = SmsGatewayError("CONNECT", uncertain=False)
    except httpx.TimeoutException:
        failure = SmsGatewayError("TIMEOUT", uncertain=True)
    except httpx.InvalidURL:
        failure = SmsGatewayError("INVALID_REQUEST", uncertain=False)
    except (httpx.HTTPError, OSError, RuntimeError):
        failure = SmsGatewayError("TRANSPORT", uncertain=True)
    # 脱离 except 后再抛出，原始异常及其带地址的消息不会挂到异常链上。
    if failure is not None:
        raise failure
    assert response is not None
    return _parse(response, phones)
