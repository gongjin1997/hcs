"""云信协议传输测试；全部请求使用 MockTransport，不联网、不发送真实短信。"""

import json
import logging
import traceback
from typing import Any

import httpx
import pytest

from app.notify import sms_gateway as gateway

ENDPOINT = "https://gateway.example.invalid/configured/HttpSmsBatchSend"
USERNAME = "synthetic-user"
PASSWORD = "synthetic-password-only"
CONTENT = "合成租户：发现告警，请人工核查。"
PHONE_A, PHONE_B = "00000000001", "00000000002"
STAMP = 1637723182356
REAL_CLIENT = httpx.Client
CODES = [0, 2, 4, 6, 7, 9, 10, 11, 30, 31, 32, 34, 41, 42, 44, 45, 46, 47, 48, 99]


def accepted(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(200, json={"code": 0, "data": [{"toPhone": row["toPhone"], "code": 0} for row in body["body"]]})


def send(client: httpx.Client | None, **overrides: Any) -> gateway.SmsBatchResult:
    arguments: dict[str, Any] = {
        "endpoint": ENDPOINT,
        "username": USERNAME,
        "password": PASSWORD,
        "content": CONTENT,
        "phones": [PHONE_A, PHONE_B],
        "batch_id": "synthetic-batch",
        "timestamp_ms": STAMP,
        "http": client,
    }
    arguments.update(overrides)
    return gateway.send_batch(**arguments)


@pytest.fixture()
def gateway_http():
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return accepted(request)

    with REAL_CLIENT(transport=httpx.MockTransport(handle), trust_env=False) as client:
        yield client, requests


@pytest.mark.parametrize(
    "username,password,stamp,expected",
    [
        ("user", "password", 1636438174323, "d8cb59e3da865a38fde94b7788236612"),
        ("测试账号", "合成口令-é", STAMP, "3691161699582e47c3ef7c95c9c1cdbf"),
    ],
)
def test_nested_md5_known_vectors_and_utf8_request_shape(gateway_http, username, password, stamp, expected):
    client, requests = gateway_http
    endpoint = ENDPOINT + "?fixed=3"
    result = send(client, endpoint=endpoint, username=username, password=password, timestamp_ms=stamp, expand_port="001")
    assert all(row.accepted for row in result.results)
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST" and str(request.url) == endpoint
    assert request.headers["Content-Type"] == "application/json;charset=utf-8"
    assert CONTENT.encode() in request.content
    payload = json.loads(request.content)
    assert payload == {
        "userName": username,
        "timestamp": stamp,
        "sign": expected,
        "content": CONTENT,
        "smsBatchId": "synthetic-batch",
        "body": [{"toPhone": PHONE_A, "expandPort": "001"}, {"toPhone": PHONE_B, "expandPort": "001"}],
    }
    assert password not in payload.values()
    assert password.encode() not in request.content
    assert request.extensions["timeout"] == {"connect": 5, "read": 5, "write": 5, "pool": 5}


def test_current_timestamp_is_unix_milliseconds(gateway_http, monkeypatch):
    client, requests = gateway_http
    monkeypatch.setattr(gateway.time, "time_ns", lambda: 1700000000123456789)
    send(client, timestamp_ms=None, timeout_seconds=2.5)
    assert json.loads(requests[0].content)["timestamp"] == 1700000000123
    assert requests[0].extensions["timeout"] == {"connect": 2.5, "read": 2.5, "write": 2.5, "pool": 2.5}


@pytest.mark.parametrize("size", [1, 500])
def test_batch_boundaries(gateway_http, size):
    client, requests = gateway_http
    phones = [f"000{index:08d}" for index in range(size)]
    result = send(client, phones=phones)
    assert [row.phone for row in result.results] == phones
    assert len(json.loads(requests[0].content)["body"]) == size


@pytest.mark.parametrize("port", [None, "", "001", "0" * 14])
def test_expand_port_is_optional_and_preserves_leading_zeroes(gateway_http, port):
    client, requests = gateway_http
    send(client, expand_port=port)
    for row in json.loads(requests[0].content)["body"]:
        assert row == {"toPhone": row["toPhone"], **({"expandPort": port} if port else {})}


@pytest.mark.parametrize(
    "field,value",
    [
        ("phones", []),
        ("phones", [f"000{i:08d}" for i in range(501)]),
        ("phones", [PHONE_A, PHONE_A]),
        ("phones", [""]),
        ("phones", [" "]),
        ("phones", [123]),
        ("phones", [True]),
        ("phones", None),
        ("expand_port", "0" * 15),
        ("expand_port", 1),
        ("timestamp_ms", True),
        ("timestamp_ms", -1),
        ("timestamp_ms", 1.5),
        ("timestamp_ms", 2**63),
        ("verify_tls", "false"),
        ("timeout_seconds", 0),
        ("timeout_seconds", -1),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", float("inf")),
        ("timeout_seconds", True),
        ("timeout_seconds", None),
        ("username", ""),
        ("password", ""),
        ("content", " "),
        ("batch_id", ""),
        ("endpoint", ""),
        ("endpoint", "/HttpSmsBatchSend"),
        ("endpoint", "file:///tmp/sms"),
        ("endpoint", "https://user:password@example.invalid/send"),
        ("endpoint", ENDPOINT + "#fragment"),
        ("endpoint", ENDPOINT + "\n"),
        ("content", "\ud800"),
        ("password", "\ud800"),
    ],
)
def test_invalid_request_does_not_reach_transport(gateway_http, field, value):
    client, requests = gateway_http
    with pytest.raises(gateway.SmsGatewayError) as error:
        send(client, **{field: value})
    assert error.value.reason_code == "INVALID_REQUEST" and not error.value.uncertain
    assert requests == []


def test_transport_does_not_apply_business_template_approval_policy(gateway_http):
    client, requests = gateway_http
    content = "合成测试内容 https://example.invalid/detail，联系电话为测试占位符"
    send(client, content=content)
    assert json.loads(requests[0].content)["content"] == content


def test_partial_rejection_and_string_zero_never_mark_entire_batch_accepted():
    phones = [PHONE_A, "bad-number"]
    response = {
        "code": "0",
        "message": "untrusted ignored text",
        "data": [
            {"toPhone": "bad-number", "code": "32", "message": "raw error ignored"},
            {"toPhone": PHONE_A, "code": "0", "msgId": 9223372036854775807},
        ],
    }
    with REAL_CLIENT(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)), trust_env=False
    ) as client:
        result = send(client, phones=phones)
    assert result.code == "0"
    assert result.results == [
        gateway.SmsRecipientResult(PHONE_A, True, "0", "9223372036854775807"),
        gateway.SmsRecipientResult("bad-number", False, "32"),
    ]


@pytest.mark.parametrize("code", CODES)
@pytest.mark.parametrize("as_string", [False, True])
def test_every_documented_root_code_is_normalized_without_raw_message(code, as_string):
    payload: dict[str, Any] = {"code": str(code) if as_string else code, "message": PASSWORD}
    if code == 0:
        payload["data"] = [{"toPhone": PHONE_A, "code": 0}, {"toPhone": PHONE_B, "code": 0}]
    with REAL_CLIENT(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), trust_env=False
    ) as client:
        result = send(client)
    assert result.code == str(code)
    assert all(row.accepted == (code == 0) and row.code == str(code) for row in result.results)
    assert PASSWORD not in repr(result)


@pytest.mark.parametrize("code", CODES)
def test_per_phone_codes_are_independent_even_when_root_succeeds(code):
    payload = {"code": 0, "data": [{"toPhone": PHONE_A, "code": str(code)}, {"toPhone": PHONE_B, "code": 0}]}
    with REAL_CLIENT(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), trust_env=False
    ) as client:
        result = send(client)
    assert result.results[0].accepted == (code == 0) and result.results[0].code == str(code)
    assert result.results[1].accepted


@pytest.mark.parametrize("message_id", [None, 0, 300000000093, 9223372036854775807, -1, "300000000093", "provider-id"])
def test_message_ids_are_optional_and_preserved_as_strings(message_id):
    payload = {"code": 0, "data": [{"toPhone": PHONE_A, "code": 0, "msgId": message_id}]}
    with REAL_CLIENT(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), trust_env=False
    ) as client:
        result = send(client, phones=[PHONE_A])
    assert result.results[0].message_id == (str(message_id) if message_id is not None else None)
    assert result.results[0].accepted


@pytest.mark.parametrize("code", [True, False, None, "", "success", "0.0", 0.0, [], {}, -1, 12, 999, "999"])
@pytest.mark.parametrize("level", ["root", "recipient"])
def test_invalid_or_unknown_codes_make_whole_response_uncertain(code, level):
    payload = {
        "code": code if level == "root" else 0,
        "data": [{"toPhone": PHONE_A, "code": 0}, {"toPhone": PHONE_B, "code": code if level == "recipient" else 0}],
    }
    with REAL_CLIENT(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), trust_env=False
    ) as client:
        with pytest.raises(gateway.SmsGatewayError) as error:
            send(client)
    assert error.value.reason_code == "INVALID_RESPONSE" and error.value.uncertain


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        [],
        [None, {}],
        [{"toPhone": PHONE_A, "code": 0}],
        [{"toPhone": PHONE_A, "code": 0}, {"toPhone": PHONE_A, "code": 0}],
        [{"toPhone": PHONE_A, "code": 0}, {"toPhone": "unrequested", "code": 0}],
        [{"toPhone": PHONE_A, "code": 0}, {"toPhone": PHONE_B}],
        [{"toPhone": PHONE_A, "code": 0}, {"code": 0}],
        [{"toPhone": PHONE_A, "code": 0}, {"toPhone": 2, "code": 0}],
    ],
)
def test_missing_duplicate_or_unrequested_results_never_return_partial_success(data):
    with REAL_CLIENT(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0, "data": data})), trust_env=False
    ) as client:
        with pytest.raises(gateway.SmsGatewayError) as error:
            send(client)
    assert error.value.reason_code == "INVALID_RESPONSE" and error.value.uncertain


@pytest.mark.parametrize("message_id", [True, False, 1.5, [], {}, "", " "])
def test_malformed_message_id_after_valid_row_rejects_entire_response(message_id):
    payload = {"code": 0, "data": [{"toPhone": PHONE_A, "code": 0}, {"toPhone": PHONE_B, "code": 0, "msgId": message_id}]}
    with REAL_CLIENT(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), trust_env=False
    ) as client:
        with pytest.raises(gateway.SmsGatewayError) as error:
            send(client)
    assert error.value.uncertain


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not-json",
        b"null",
        b"[]",
        b"{}",
        b'"text"',
        b"\xff",
        b'{"code":2,"code":0,"data":[]}',
        b'{"code":NaN}',
        b'{"code":Infinity}',
    ],
)
def test_invalid_json_and_duplicate_keys_are_uncertain_and_do_not_retain_raw_response(raw):
    with REAL_CLIENT(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw)), trust_env=False
    ) as client:
        with pytest.raises(gateway.SmsGatewayError) as error:
            send(client)
    assert error.value.reason_code == "INVALID_RESPONSE" and error.value.uncertain
    assert error.value.__context__ is None and error.value.__cause__ is None


@pytest.mark.parametrize(
    "status,reason,uncertain",
    [
        (401, "AUTH", False),
        (403, "AUTH", False),
        (400, "HTTP_ERROR", True),
        (429, "HTTP_ERROR", True),
        (500, "HTTP_ERROR", True),
        (503, "HTTP_ERROR", True),
    ],
)
def test_http_errors_are_safe_and_never_turn_gateway_body_into_success(status, reason, uncertain):
    with REAL_CLIENT(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"code": 0, "message": PASSWORD})),
        trust_env=False,
    ) as client:
        with pytest.raises(gateway.SmsGatewayError) as error:
            send(client)
    assert error.value.reason_code == reason and error.value.uncertain is uncertain
    assert PASSWORD not in str(error.value) and ENDPOINT not in repr(error.value)


@pytest.mark.parametrize(
    "exception,reason,uncertain",
    [
        (httpx.ConnectError, "CONNECT", False),
        (httpx.ConnectTimeout, "TIMEOUT", True),
        (httpx.ReadTimeout, "TIMEOUT", True),
        (httpx.WriteTimeout, "TIMEOUT", True),
        (httpx.PoolTimeout, "TIMEOUT", True),
        (httpx.ReadError, "TRANSPORT", True),
        (httpx.WriteError, "TRANSPORT", True),
        (httpx.RemoteProtocolError, "TRANSPORT", True),
        (RuntimeError, "TRANSPORT", True),
    ],
)
def test_transport_errors_are_redacted_have_no_original_chain_and_never_retry(exception, reason, uncertain, caplog):
    calls = []

    def handle(request):
        calls.append(request)
        raise exception(f"{ENDPOINT} {USERNAME} {PASSWORD} {CONTENT} " + json.loads(request.content)["sign"])

    with REAL_CLIENT(transport=httpx.MockTransport(handle), trust_env=False) as client:
        with caplog.at_level(logging.DEBUG, logger=gateway.__name__), pytest.raises(gateway.SmsGatewayError) as error:
            send(client)
        assert not client.is_closed
    assert len(calls) == 1
    assert error.value.reason_code == reason and error.value.uncertain is uncertain
    assert error.value.__context__ is None and error.value.__cause__ is None
    exposed = str(error.value) + repr(error.value) + "".join(traceback.format_exception(error.value)) + caplog.text
    sign = json.loads(calls[0].content)["sign"]
    assert all(value not in exposed for value in (ENDPOINT, USERNAME, PASSWORD, CONTENT, sign))


@pytest.mark.parametrize("tls", [True, False])
def test_owned_client_applies_tls_no_proxy_policy_and_closes(monkeypatch, tls):
    instances: list[httpx.Client] = []
    options: list[dict[str, Any]] = []

    def factory(**kwargs):
        options.append(kwargs.copy())
        client = REAL_CLIENT(transport=httpx.MockTransport(accepted), **kwargs)
        instances.append(client)
        return client

    monkeypatch.setattr(gateway.httpx, "Client", factory)
    result = send(None, verify_tls=tls)
    assert all(row.accepted for row in result.results)
    assert options == [{"verify": tls, "timeout": 5, "follow_redirects": False, "trust_env": False}]
    assert len(instances) == 1 and instances[0].is_closed


@pytest.mark.parametrize("mode", ["timeout", "malformed"])
def test_owned_client_closes_after_transport_or_parse_failure(monkeypatch, mode):
    instances: list[httpx.Client] = []
    requests: list[httpx.Request] = []

    def handle(request):
        requests.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout(PASSWORD)
        return httpx.Response(200, content=PASSWORD.encode())

    def factory(**kwargs):
        client = REAL_CLIENT(transport=httpx.MockTransport(handle), **kwargs)
        instances.append(client)
        return client

    monkeypatch.setattr(gateway.httpx, "Client", factory)
    with pytest.raises(gateway.SmsGatewayError):
        send(None)
    assert len(requests) == len(instances) == 1 and instances[0].is_closed


def test_injected_client_is_reusable_and_redirect_policy_is_not_mutated():
    calls: list[httpx.Request] = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/probe":
            return httpx.Response(200)
        return httpx.Response(307, headers={"Location": "https://other.example.invalid/never"})

    with REAL_CLIENT(transport=httpx.MockTransport(handle), trust_env=False, follow_redirects=True) as client:
        with pytest.raises(gateway.SmsGatewayError) as error:
            send(client)
        assert error.value.reason_code == "HTTP_ERROR" and error.value.uncertain
        assert len(calls) == 1 and not client.is_closed and client.follow_redirects
        assert client.get("https://gateway.example.invalid/probe").status_code == 200
    assert len(calls) == 2 and all(request.url.host == "gateway.example.invalid" for request in calls)


def test_success_repr_never_includes_body_phone_ids_or_response_message():
    payload = {
        "code": "0",
        "message": PASSWORD,
        "data": [{"toPhone": PHONE_A, "code": "0", "msgId": "synthetic-message-id", "message": CONTENT}],
    }
    with REAL_CLIENT(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), trust_env=False
    ) as client:
        result = send(client, phones=[PHONE_A])
    assert result.results[0].accepted and result.results[0].message_id == "synthetic-message-id"
    assert all(value not in repr(result) for value in (PHONE_A, "synthetic-message-id", PASSWORD, CONTENT, USERNAME))
