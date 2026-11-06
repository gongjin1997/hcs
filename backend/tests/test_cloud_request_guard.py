"""低扰动云调用：只使用 MockTransport，不访问云、LAN 或生产配置。"""
import gzip
import json
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from app.core.config import get_settings
from app.core.logging import JsonFormatter
from app.hcs import auth as auth_module
from app.hcs import ratelimit
from app.hcs.auth import AkSkAuth, TokenAuth, TokenProvider
from app.hcs.client import HcsClient
from app.hcs.diagnostics import retry_after
from app.hcs.endpoints import EndpointResolver
from app.hcs.errors import HcsError
from app.hcs.pager import page_all
from app.hcs.ratelimit import request_budget
from app.metrics.ces import ces_query
from app.metrics.prometheus import PromClient, PromError


@pytest.fixture(autouse=True)
def isolated_limits(monkeypatch):
    # PG 的 Alembic fileConfig 会禁用已存在 logger；本文件明确启用被测日志器。
    for name in ("app.hcs.client", "app.hcs.auth", "app.metrics.prometheus"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "disabled", False)
        monkeypatch.setattr(logger, "propagate", True)
    monkeypatch.setattr(ratelimit, "_shared", {})
    monkeypatch.setattr(auth_module, "_tokens", OrderedDict())
    monkeypatch.setattr(get_settings(), "redis_url", None)
    monkeypatch.setattr(get_settings(), "hcs_service_concurrency", 2)
    monkeypatch.setattr(get_settings(), "hcs_max_attempts", 3)
    monkeypatch.setattr(get_settings(), "hcs_acquire_timeout_seconds", 0.5)
    monkeypatch.setattr(get_settings(), "hcs_total_qps", 1000)


def client(http, *, tenant=101, project=1001, rate=1000, auth=None, retries=0, concurrency=None):
    return HcsClient(http, auth or AkSkAuth("test-ak", "test-sk"),
                     EndpointResolver({"dcs": "https://cloud.invalid", "ces": "https://cloud.invalid"}, "test", "invalid"),
                     str(project), tenant_id=tenant, project_ref=project, qps={"dcs": rate, "ces": rate},
                     max_retries=retries, backoff=0, max_concurrency=concurrency)


def test_new_clients_share_smooth_rate_without_refilling_or_initial_burst():
    starts = []
    with httpx.Client(transport=httpx.MockTransport(lambda _r: (starts.append(time.monotonic()) or
                                                               httpx.Response(200, json={"ok": True})))) as http:
        for i in range(5):
            current = client(http, tenant=i + 1, project=i + 10, rate=25)
            current.configure_qps({"dcs": 25})
            current.get("dcs", "/v2/{project_id}/instances")
    assert len(starts) == 5
    assert all(b - a >= 0.036 for a, b in zip(starts, starts[1:], strict=False))


def test_default_client_cannot_raise_runtime_shared_qps_but_explicit_update_can():
    starts = []
    with httpx.Client(transport=httpx.MockTransport(lambda _r: starts.append(time.monotonic()) or
                                                   httpx.Response(200, json={}))) as http:
        configured = client(http)
        configured.configure_qps({"dcs": 20})
        configured.get("dcs", "/instances")
        client(http, rate=1000).get("dcs", "/instances")
        configured.configure_qps({"dcs": 1000})
        configured.get("dcs", "/instances")
        assert starts[1] - starts[0] >= 0.045
        assert starts[2] - starts[1] >= 0.045  # 升额不清除上一次请求留下的间隔。


def test_clients_cannot_raise_global_concurrency_and_lower_active_limit_is_honoured():
    active = peak = 0
    lock = threading.Lock()
    def respond(_request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return httpx.Response(200, json={})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http, ThreadPoolExecutor(max_workers=8) as pool:
        clients = [client(http, tenant=i, concurrency=8) for i in range(8)]
        list(pool.map(lambda c: c.get("dcs", "/instances"), clients))
    assert peak == 2 and active == 0

    entered, release, calls = threading.Event(), threading.Event(), []
    def gated(_request):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        return httpx.Response(200, json={})
    with httpx.Client(transport=httpx.MockTransport(gated)) as http, ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client(http, concurrency=1).get, "dcs", "/instances")
        assert entered.wait(2)
        second = pool.submit(client(http, concurrency=8).get, "dcs", "/instances")
        time.sleep(0.04)
        assert len(calls) == 1
        release.set()
        assert first.result() == second.result() == {}


def test_401_and_transient_errors_share_total_attempt_budget_and_preserve_safe_codes(caplog):
    class Auth:
        kind = "TEST"
        refreshes = 0
        def apply(self, _request, _service):
            pass
        def refresh(self):
            self.refreshes += 1
            return True
    statuses = iter([401, 503, 503, 200])
    calls = []
    def respond(_request):
        status = next(statuses)
        calls.append(status)
        return httpx.Response(status, json={"error_code": "CES.0012", "message": "unlabelled-private-marker"},
                              headers={"X-Request-Id": "request-test-123"})
    auth = Auth()
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        current = client(http, auth=auth, retries=None)
        with pytest.raises(HcsError) as caught:
            current.get("dcs", "/instances", params={"password": "private-query-marker"})
        assert current.calls == 3 and calls == [401, 503, 503] and auth.refreshes == 1
        assert caught.value.code == "CES.0012" and caught.value.request_id == "request-test-123"
    output = "\n".join(JsonFormatter("scheduler").format(r) for r in caplog.records)
    assert "unlabelled-private-marker" not in output and "private-query-marker" not in output
    assert all(r.cloud_service == "dcs" and r.tenant_id == 101 and r.project_ref == 1001 for r in caplog.records
               if r.msg == "cloud.request.failed")


def test_retry_after_is_shared_and_a_long_delay_does_not_sleep_or_send_again():
    calls = []
    def respond(_request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "60"}, json={"error_code": "APIG.0308"})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        started = time.monotonic()
        with pytest.raises(HcsError):
            client(http, retries=None).get("dcs", "/instances")
        with pytest.raises(HcsError, match="限速"):
            client(http).get("dcs", "/instances")
    assert len(calls) == 1 and time.monotonic() - started < 1
    assert 4 <= retry_after(format_datetime(datetime.now(UTC) + timedelta(seconds=5))) <= 5
    assert retry_after("not-a-date") is None


def test_only_read_queries_are_sent_and_redirects_are_not_followed():
    calls = []
    def respond(request):
        calls.append(request.method)
        return httpx.Response(302, headers={"Location": "https://other.invalid/secret"})
    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as http:
        current = client(http)
        for method, path in (("DELETE", "/instances"), ("POST", "/instances"), ("PUT", "/instances")):
            with pytest.raises(HcsError, match="只允许"):
                current.request("dcs", method, path)
        with pytest.raises(HcsError) as error:
            current.get("dcs", "/instances")
    assert calls == ["GET"] and error.value.status == 302


def test_request_budget_counts_iam_and_service_attempts_without_extra_network():
    calls = []
    def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/v3/auth/tokens":
            return httpx.Response(201, headers={"X-Subject-Token": "test-only-token"},
                                  json={"token": {"expires_at": "2030-01-01T00:00:00Z"}})
        return httpx.Response(200, json={})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        provider = TokenProvider(http, "https://iam.invalid", "test", "reader", "test-only-value", "1001")
        current = client(http, auth=TokenAuth(provider))
        with request_budget(attempts=2):
            current.get("dcs", "/instances")
            with pytest.raises(HcsError, match="预算"):
                current.get("dcs", "/instances")
        snapshot = current.metrics_snapshot()
    assert calls == ["/v3/auth/tokens", "/instances"] and snapshot["calls"] == 2
    assert snapshot["services"]["iam"]["calls"] == snapshot["services"]["dcs"]["calls"] == 1


def test_iam_tokens_and_forced_checks_share_only_identical_scope_and_credentials():
    calls = []
    def respond(_request):
        calls.append(1)
        time.sleep(0.02)
        return httpx.Response(201, headers={"X-Subject-Token": f"test-token-{len(calls)}"},
                              json={"token": {"expires_at": "2030-01-01T00:00:00Z"}})
    def provider(http, tenant=101, project=1001, password="synthetic-password"):
        return TokenProvider(http, "https://iam.invalid", "domain", "reader", password, str(project),
                             tenant_id=tenant, project_ref=project)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http, ThreadPoolExecutor(max_workers=6) as pool:
        providers = [provider(http) for _ in range(6)]
        assert len(set(pool.map(lambda p: p.get(force=True), providers))) == 1
        assert len(calls) == 1
        assert provider(http).get() == providers[0].get()
        provider(http, tenant=202).get()
        provider(http, project=1002).get()
        provider(http, password="changed-synthetic-password").get()
        assert len(calls) == 4
        old = providers[0].get()
        assert providers[0].get(force=True, refresh=True) != old
        assert providers[1].get(force=True, refresh=True) == providers[0].get()
        assert len(calls) == 5


def test_iam_failure_is_sanitized_and_short_cached_without_logging_auth_config(caplog):
    calls = []
    def respond(_request):
        calls.append(1)
        return httpx.Response(401, json={"error_code": "IAM.0101", "error_msg": "plain-private-response"})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        for _ in range(3):
            provider = TokenProvider(http, "https://iam.invalid", "domain", "reader", "private-password", "project")
            with pytest.raises(HcsError, match="IAM.0101") as error:
                provider.get(force=True)
            assert "plain-private-response" not in str(error.value)
    assert len(calls) == 1
    output = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert "private-password" not in output and "plain-private-response" not in output


def test_network_diagnostics_do_not_expose_urls_queries_or_exception_payload(caplog):
    def respond(request):
        raise httpx.ConnectError("https://name:private-password@private.invalid/?token=private-query raw-private-payload",
                                 request=request)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(HcsError) as error:
            client(http).get("dcs", "/instances")
    output = "\n".join(JsonFormatter().format(r) for r in caplog.records) + str(error.value)
    assert "private-password" not in output and "private-query" not in output and "raw-private-payload" not in output
    assert caplog.records[-1].exception_type == "ConnectError"


def test_prometheus_uses_shared_limits_and_hides_upstream_error_body(caplog):
    calls = []
    def respond(_request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "60"}, json={"error": "prom-private-data"})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        first = PromClient("https://prom.invalid", http, tenant_id=101, project_ref=1001)
        with pytest.raises(PromError):
            first.query('private_query{token="private-value"}')
        with pytest.raises(PromError):
            PromClient("https://prom.invalid", http).query("up")
    assert calls == [1]
    output = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert all(value not in output for value in ("prom-private-data", "private_query", "private-value"))


def test_repeated_pages_and_large_metric_sets_fail_before_amplifying_queries():
    calls = []
    def fetch(_params):
        calls.append(1)
        return {"items": [{"id": "one"}], "total": 100000}
    with pytest.raises(HcsError, match="重复"):
        list(page_all(fetch, "items", limit=1))
    assert len(calls) == 2
    with httpx.Client(transport=httpx.MockTransport(lambda _r: pytest.fail("unexpected upstream call"))) as http:
        with pytest.raises(HcsError, match="预算"):
            ces_query(client(http), [{"metric_name": str(i)} for i in range(10001)])


def test_diagnostic_fields_are_structured_and_service_remains_process_identity(caplog):
    caplog.set_level(logging.DEBUG, logger="app.hcs.client")
    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"ok": True},
                      headers={"X-Openstack-Request-Id": "req-test-123"}))) as http:
        current = client(http)
        current.get("dcs", "/v2/{project_id}/instances/private-resource-id", params={"token": "test-private-query"})
        current.metrics_snapshot()
    record = next(r for r in caplog.records if r.msg == "cloud.request.ok")
    data = json.loads(JsonFormatter("scheduler").format(record))
    assert data["service"] == "scheduler" and data["cloud_service"] == "dcs"
    assert data["logical_path"] == "/v2/{project_id}/instances/{item}"
    assert {"status", "attempt", "cost_ms", "wait_ms", "tenant_id", "project_ref", "upstream_request_id"} <= data.keys()
    assert data["upstream_request_id"] == "req-test-123"
    assert "test-private-query" not in json.dumps(data) and "private-resource-id" not in json.dumps(data)


def test_compressed_success_response_is_read_once_without_double_decoding():
    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(
            200, content=gzip.compress(b'{"ok":true}'), headers={"Content-Encoding": "gzip"}))) as http:
        assert client(http).get("dcs", "/instances") == {"ok": True}


def test_aksk_authentication_failure_has_fixed_diagnosis_without_response_message():
    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(
            401, json={"error_code": "APIGW.0301", "message": "synthetic-private-upstream-text"}))) as http:
        with pytest.raises(HcsError, match="签名不匹配") as error:
            client(http).get("dcs", "/instances")
    assert error.value.code == "APIGW.0301" and "synthetic-private-upstream-text" not in str(error.value)
