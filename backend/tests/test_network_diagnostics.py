import errno
import json
import logging
import socket
import ssl

import httpcore
import httpx
import pytest

from app.core.logging import JsonFormatter
from app.hcs.diagnostics import network_error, record_attempt

PRIVATE = "https://proxy-user:private-password@private.invalid/tenant-private?token=private-token response-private"


def wrapped(cause: BaseException, *, context: bool = False) -> httpx.ConnectError:
    core = httpcore.ConnectError(PRIVATE)
    outer = httpx.ConnectError(PRIVATE, request=httpx.Request("GET", PRIVATE))
    if context:
        core.__context__ = cause
        outer.__context__ = core
    else:
        core.__cause__ = cause
        outer.__cause__ = core
    return outer


@pytest.mark.parametrize("root,hint", [
    (ssl.SSLCertVerificationError(1, PRIVATE), "TLS 证书校验失败"),
    (ssl.SSLError(1, PRIVATE), "TLS 握手失败"),
    (socket.gaierror(socket.EAI_NONAME, PRIVATE), "上游服务域名解析失败"),
    (socket.gaierror(socket.EAI_AGAIN, PRIVATE), "上游服务域名解析失败"),
    (OSError(errno.ECONNREFUSED, PRIVATE), "上游端口拒绝连接"),
    (OSError(errno.ENETUNREACH, PRIVATE), "上游网络不可达"),
    (OSError(errno.EHOSTUNREACH, PRIVATE), "上游网络不可达"),
    (OSError(errno.ENETDOWN, PRIVATE), "上游网络不可达"),
    (OSError(errno.EACCES, PRIVATE), "网络访问被拒绝"),
    (OSError(errno.EPERM, PRIVATE), "网络访问被拒绝"),
    (OSError(errno.ECONNRESET, PRIVATE), "上游连接被中断"),
    (OSError(errno.ECONNABORTED, PRIVATE), "上游连接被中断"),
    (OSError(errno.EPIPE, PRIVATE), "上游连接被中断"),
    (OSError(errno.ETIMEDOUT, PRIVATE), "上游网络请求超时"),
    (TimeoutError(PRIVATE), "上游网络请求超时"),
    (OSError(10061, PRIVATE), "上游端口拒绝连接"),
    (OSError(10051, PRIVATE), "上游网络不可达"),
    (OSError(10065, PRIVATE), "上游网络不可达"),
    (OSError(10054, PRIVATE), "上游连接被中断"),
])
@pytest.mark.parametrize("context", [False, True], ids=["cause", "context"])
def test_wrapped_system_errors_are_classified_without_private_content(root, hint, context):
    result = network_error(wrapped(root, context=context))
    assert result.status == 0 and result.code == "NETWORK" and result.request_id is None
    assert result.message.startswith(hint) or hint in result.message
    assert "请检查" in result.message or "请核对" in result.message
    assert all(value not in str(result) for value in ("private", "https://", "proxy-user", "response-", "tenant-"))
    assert result.__cause__ is None and result.__context__ is None


@pytest.mark.parametrize("namespace", [httpx, httpcore], ids=["httpx", "httpcore"])
@pytest.mark.parametrize("kind,hint", [
    ("ConnectTimeout", "连接上游服务超时"),
    ("ReadTimeout", "读取上游响应超时"),
    ("WriteTimeout", "发送上游请求超时"),
    ("PoolTimeout", "等待上游连接池超时"),
    ("TimeoutException", "上游网络请求超时"),
    ("ProxyError", "上游代理连接失败"),
])
def test_httpx_and_httpcore_timeout_phases_and_proxy(namespace, kind, hint):
    result = network_error(wrapped(getattr(namespace, kind)(PRIVATE)), iam=True)
    assert result.status == 0 and result.code == "IAM.NETWORK"
    assert result.message.startswith(hint)
    assert "private" not in str(result) and "proxy-user" not in str(result)


@pytest.mark.parametrize("message,hint", [
    ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", "TLS 证书校验失败"),
    ("Hostname mismatch", "TLS 证书校验失败"),
    ("certificate has expired", "TLS 证书校验失败"),
    ("Temporary failure in name resolution", "上游服务域名解析失败"),
    ("Name or service not known", "上游服务域名解析失败"),
    ("nodename nor servname provided, or not known", "上游服务域名解析失败"),
    ("No address associated with hostname", "上游服务域名解析失败"),
    ("Connection refused", "上游端口拒绝连接"),
    ("No route to host", "上游网络不可达"),
    ("Network is unreachable", "上游网络不可达"),
    ("Connection reset by peer", "上游连接被中断"),
    ("[SSL: WRONG_VERSION_NUMBER] wrong version number", "TLS 握手失败"),
    ("Tunnel connection failed", "上游代理连接失败"),
    ("The operation timed out", "上游网络请求超时"),
])
def test_text_fallback_classifies_only_and_never_echoes(message, hint):
    result = network_error(wrapped(RuntimeError(f"{message}: {PRIVATE}")))
    assert hint in result.message
    assert PRIVATE not in str(result) and "private" not in str(result) and "proxy-user" not in str(result)


def test_typed_root_wins_over_outer_text_and_generic_timeout():
    outer = httpx.ConnectTimeout("Connection refused: " + PRIVATE)
    outer.__cause__ = wrapped(ssl.SSLCertVerificationError(1, PRIVATE))
    assert network_error(outer).message.startswith("TLS 证书校验失败")
    outer.__cause__ = wrapped(OSError(errno.ENETUNREACH, PRIVATE))
    assert network_error(outer).message.startswith("上游网络不可达")


def test_timeout_phase_is_preserved_with_system_timeout_cause():
    core = httpcore.ConnectTimeout(PRIVATE)
    core.__cause__ = TimeoutError(PRIVATE)
    assert network_error(wrapped(core)).message.startswith("连接上游服务超时")


def test_both_cause_and_context_are_inspected_without_mutating_original():
    outer = wrapped(RuntimeError(PRIVATE))
    outer.__context__ = socket.gaierror(socket.EAI_NONAME, PRIVATE)
    result = network_error(outer)
    assert "上游服务域名解析失败" in result.message
    assert isinstance(outer.__cause__, httpcore.ConnectError)
    assert isinstance(outer.__context__, socket.gaierror)


def test_unknown_and_cyclic_errors_have_fixed_safe_fallback():
    outer = wrapped(RuntimeError(PRIVATE))
    assert outer.__cause__ is not None
    outer.__cause__.__context__ = outer
    outer.__context__ = outer
    assert network_error(outer).message == "上游网络请求失败，请检查容器网络、服务地址和端口"


def test_chain_work_and_string_inspection_are_bounded():
    inspected = []

    class CountedError(RuntimeError):
        def __str__(self):
            inspected.append(1)
            return PRIVATE

    outer = httpx.ConnectError(PRIVATE)
    current: BaseException = outer
    for _ in range(100):
        current.__cause__ = CountedError()
        current = current.__cause__
    current.__cause__ = ssl.SSLCertVerificationError(1, PRIVATE)
    assert network_error(outer).message == "上游网络请求失败，请检查容器网络、服务地址和端口"
    assert 0 < len(inspected) < 16


def test_fallback_inspects_bounded_text_only():
    outer = httpx.ConnectError("x" * 2048 + " certificate_verify_failed " + PRIVATE)
    assert network_error(outer).message == "上游网络请求失败，请检查容器网络、服务地址和端口"


def test_broken_string_representation_keeps_safe_fallback():
    class BrokenError(RuntimeError):
        def __str__(self):
            raise ValueError(PRIVATE)

    assert network_error(wrapped(BrokenError())).message == "上游网络请求失败，请检查容器网络、服务地址和端口"


def test_system_errno_wins_over_untrusted_text_fallback():
    result = network_error(wrapped(OSError(errno.ECONNREFUSED, "certificate_verify_failed " + PRIVATE)))
    assert result.message.startswith("上游端口拒绝连接")


@pytest.mark.parametrize("iam", [False, True])
def test_classified_error_logging_never_includes_exception_chain_or_request(caplog, iam):
    source = wrapped(ssl.SSLCertVerificationError(1, PRIVATE))
    result = network_error(source, iam=iam)
    with caplog.at_level(logging.WARNING):
        record_attempt(logging.getLogger("network.test"), service="iam" if iam else "dcs", path="/v3/auth/tokens",
                       method="POST", tenant_id=None, project_ref=None, status=0, attempt=1, cost_ms=1, wait_ms=0,
                       error=result, exception_type=type(source).__name__)
    output = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert "TLS 证书校验失败" in output
    assert all(value not in output for value in ("private", "https://", "proxy-user", "response-", "tenant-"))
    data = json.loads(JsonFormatter().format(caplog.records[-1]))
    assert data["exception_type"] == "ConnectError" and data["status"] == 0
    assert ("IAM.NETWORK" if iam else "NETWORK") in data["exc"]


@pytest.mark.parametrize("root,hint", [
    (socket.gaierror(socket.EAI_AGAIN, PRIVATE), "上游服务域名解析失败"),
    (OSError(errno.ECONNREFUSED, PRIVATE), "上游端口拒绝连接"),
    (OSError(errno.ENETUNREACH, PRIVATE), "上游网络不可达"),
    (TimeoutError(PRIVATE), "连接上游服务超时"),
    (ssl.SSLCertVerificationError(1, PRIVATE), "TLS 证书校验失败"),
])
def test_real_transport_wrapping_is_classified_without_network(monkeypatch, root, hint):
    def fail(*args, **kwargs):
        raise root

    monkeypatch.setattr(socket, "create_connection", fail)
    with httpx.Client(trust_env=False) as client:
        with pytest.raises(httpx.HTTPError) as captured:
            client.get("https://network-test.invalid")
    assert captured.value.__cause__ is not None
    assert isinstance(captured.value.__cause__, (httpcore.ConnectError, httpcore.ConnectTimeout))
    core = captured.value.__cause__
    assert (core.__cause__ or core.__context__) is root
    assert hint in network_error(captured.value).message
