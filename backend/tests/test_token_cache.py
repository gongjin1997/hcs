"""IAM Token 的真实时效与共享；合成凭据和 MockTransport，不访问真实云。"""
import base64
import hashlib
import json
import logging
import multiprocessing
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
import redis

from app.core.config import get_settings
from app.core.crypto import PREFIX, decrypt
from app.core.logging import JsonFormatter
from app.hcs import auth as auth_module
from app.hcs import ratelimit
from app.hcs.auth import TokenAuth, TokenProvider
from app.hcs.client import HcsClient
from app.hcs.endpoints import EndpointResolver
from app.hcs.errors import HcsError

from .test_cloud_shared_redis import shared_redis as shared_redis

SYNTHETIC_MASTER = base64.b64encode(bytes(range(32))).decode()


@pytest.fixture(autouse=True)
def isolated_tokens(monkeypatch):
    monkeypatch.setattr(auth_module, "_tokens", OrderedDict())
    monkeypatch.setattr(ratelimit, "_shared", {})
    monkeypatch.setattr(get_settings(), "redis_url", None)
    monkeypatch.setattr(get_settings(), "hcs_total_qps", 1000)
    for name in ("app.hcs.auth", "app.hcs.token_cache", "app.hcs.client"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)
        monkeypatch.setattr(logging.getLogger(name), "propagate", True)


def provider(http, *, tenant=101, project=1001, password="synthetic-password", endpoint="https://iam.invalid",
             endpoint_scope=""):
    return TokenProvider(http, endpoint, "synthetic-domain", "synthetic-reader", password, str(project),
                         tenant_id=tenant, project_ref=project, endpoint_scope=endpoint_scope)


@pytest.mark.parametrize("ttl", [1, 30, 300, 3600])
def test_short_lived_tokens_are_reused_and_refresh_once_at_adaptive_boundary(monkeypatch, ttl):
    clock, calls = [time.time()], []
    monkeypatch.setattr(auth_module.time, "time", lambda: clock[0])
    def respond(_request):
        calls.append(1)
        return httpx.Response(201, headers={"X-Subject-Token": f"synthetic-token-{len(calls)}"},
                              json={"token": {"expires_at": datetime.fromtimestamp(clock[0] + ttl, UTC).isoformat()}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        first = provider(http).get()
        clock[0] += ttl * 0.5
        assert all(provider(http).get() == first for _ in range(30))
        assert calls == [1]
        clock[0] += ttl * 0.41
        second = provider(http).get()
        assert second != first and calls == [1, 1]
        assert all(provider(http).get() == second for _ in range(30))
        assert calls == [1, 1]


@pytest.mark.parametrize("value", [None, 123, "", "invalid", "2030-01-01T00:00:00", "2000-01-01T00:00:00Z"])
def test_invalid_missing_naive_or_expired_expiry_is_rejected_without_guessed_lifetime(value):
    body = {"token": {}} if value is None else {"token": {"expires_at": value}}
    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(
            201, headers={"X-Subject-Token": "synthetic-not-cacheable"}, json=body))) as http:
        current = provider(http)
        with pytest.raises(HcsError) as caught:
            current.get()
        assert caught.value.code == "IAM.INVALID_EXPIRY" and current._token is None


def test_explicit_timezone_offset_is_normalized_and_preserved():
    expires = datetime.now(UTC) + timedelta(minutes=5)
    offset = expires.astimezone(timezone(timedelta(hours=9))).isoformat()
    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(
            201, headers={"X-Subject-Token": "synthetic-valid"}, json={"token": {"expires_at": offset}}))) as http:
        current = provider(http)
        current.get()
        assert abs(current._expires_at - expires.timestamp()) < 0.001


def issued_response(number, ttl=60):
    return httpx.Response(201, headers={"X-Subject-Token": f"synthetic-issued-{number}"},
                          json={"token": {"expires_at": (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()}})


def test_threads_clients_and_concurrent_force_buttons_reuse_one_short_ttl(monkeypatch):
    clock, calls = [time.time()], []
    monkeypatch.setattr(auth_module.time, "time", lambda: clock[0])
    def respond(_request):
        calls.append(1)
        time.sleep(0.02)
        return issued_response(len(calls))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http, ThreadPoolExecutor(max_workers=12) as pool:
        first = list(pool.map(lambda _: provider(http).get(), range(24)))
        assert len(set(first)) == 1 and len(calls) == 1
        forced = list(pool.map(lambda _: provider(http).get(force=True), range(24)))
        assert len(set(forced)) == 1 and forced[0] != first[0] and len(calls) == 2
        clock[0] += 3
        provider(http).get(force=True)
        assert len(calls) == 3


def test_late_401_only_rejects_the_generation_actually_sent():
    calls = []
    def respond(_request):
        calls.append(1)
        return issued_response(len(calls))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        current, other = provider(http), provider(http)
        old = current.get_record()
        new = other.get_record(force=True, refresh=True, rejected_token=old.token, rejected_generation=old.generation)
        assert new.generation != old.generation and len(calls) == 2
        late = current.get_record(force=True, refresh=True, rejected_token=old.token, rejected_generation=old.generation)
        assert late == new and len(calls) == 2


def test_client_uses_request_token_generation_instead_of_mutable_client_counter():
    issued, sent = [], []
    current = None
    def respond(request):
        if request.url.path == "/v3/auth/tokens":
            issued.append(1)
            return issued_response(len(issued))
        sent.append(request.headers["X-Auth-Token"])
        if len(sent) == 1:
            # 模拟另一个请求已更新 client 计数；不能据此跳过实际被拒绝 Token 的刷新。
            current._auth_generation += 1
            return httpx.Response(401, json={})
        return httpx.Response(200 if sent[-1] != sent[0] else 401, json={})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        current = HcsClient(http, TokenAuth(provider(http)), EndpointResolver({"dcs": "https://cloud.invalid"}, "r", "invalid"),
                            "1001", tenant_id=101, project_ref=1001, max_retries=1)
        assert current.get("dcs", "/instances") == {}
    assert len(issued) == len(sent) == 2 and current.calls == 4


def test_terminal_401_invalidates_generation_without_authenticating_outside_retry_budget():
    issued, sent = [], []
    def respond(request):
        if request.url.path == "/v3/auth/tokens":
            issued.append(1)
            return issued_response(len(issued))
        sent.append(request.headers["X-Auth-Token"])
        return httpx.Response(401 if len(sent) == 1 else 200, json={})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        current = HcsClient(http, TokenAuth(provider(http)), EndpointResolver({"dcs": "https://cloud.invalid"}, "r", "invalid"),
                            "1001", tenant_id=101, project_ref=1001, max_retries=0)
        with pytest.raises(HcsError) as caught:
            current.get("dcs", "/instances")
        assert caught.value.status == 401 and len(issued) == 1 and len(sent) == 1
        assert current.get("dcs", "/instances") == {}
    assert len(issued) == len(sent) == 2 and sent[0] != sent[1]


def test_identity_credentials_and_endpoint_changes_never_reuse_another_token():
    calls = []
    def respond(_request):
        calls.append(1)
        return issued_response(len(calls))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        tokens = [provider(http, **kwargs).get() for kwargs in ({}, {"tenant": 202}, {"project": 1002},
                  {"password": "synthetic-changed"}, {"endpoint": "https://other-iam.invalid"},
                  {"endpoint_scope": "changed-service-endpoint"})]
    assert len(set(tokens)) == len(calls) == 6


def test_factory_service_endpoint_changes_also_isolate_token_scope(monkeypatch):
    from types import SimpleNamespace

    from app.core.crypto import encrypt
    from app.hcs.factory import client_for

    monkeypatch.setattr(get_settings(), "hcs_mock_url", None)
    tenant = SimpleNamespace(id=101, code="test", iam_endpoint="https://iam.invalid", endpoints={}, auth_type="PASSWORD",
                             secret_cipher=encrypt("synthetic-password"), username="reader", domain_name="test", domain_id=None)
    project = SimpleNamespace(id=1001, project_id="project", region_id="test", external_domain="invalid")
    calls = []
    def respond(_request):
        calls.append(1)
        return issued_response(len(calls))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        first = client_for(tenant, project, http=http).auth.provider.get()
        tenant.endpoints = {"dcs": "https://changed.invalid"}
        second = client_for(tenant, project, http=http).auth.provider.get()
    assert first != second and len(calls) == 2


def test_redis_failure_keeps_local_valid_token_without_redis_per_interface(monkeypatch):
    clock, calls, redis_reads = [time.time()], [], []
    monkeypatch.setattr(auth_module.time, "time", lambda: clock[0])
    def respond(_request):
        calls.append(1)
        return issued_response(len(calls))
    class BrokenRedis:
        def hgetall(self, _key):
            redis_reads.append(1)
            raise redis.ConnectionError("synthetic-private-redis-location")
        def close(self):
            pass
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        first = provider(http).get()
        monkeypatch.setattr(get_settings(), "redis_url", "redis://127.0.0.1:1/0")
        monkeypatch.setattr(get_settings(), "master_key", SYNTHETIC_MASTER)
        monkeypatch.setattr(redis.Redis, "from_url", lambda *_a, **_kw: BrokenRedis())
        assert all(provider(http).get() == first for _ in range(30))
        assert redis_reads == []
        clock[0] += 55
        assert all(provider(http).get() == first for _ in range(30))
        assert len(redis_reads) == 1 and len(calls) == 1
        clock[0] += 6
        with pytest.raises(HcsError, match="共享 IAM"):
            provider(http).get()
        with pytest.raises(HcsError, match="共享 IAM"):
            provider(http, tenant=202).get()
        assert len(calls) == 1


@pytest.mark.parametrize("master", [None, "invalid", base64.b64encode(b"too-short").decode()])
def test_redis_missing_master_key_refuses_to_authenticate(monkeypatch, master):
    monkeypatch.setattr(get_settings(), "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(get_settings(), "master_key", master)
    with httpx.Client(transport=httpx.MockTransport(lambda _r: pytest.fail("unexpected IAM request"))) as http:
        with pytest.raises(HcsError) as caught:
            provider(http).get()
    assert caught.value.code == "IAM.CACHE_KEY"


def test_rejected_token_is_not_used_as_redis_failure_fallback(monkeypatch):
    calls = []
    def respond(_request):
        calls.append(1)
        return issued_response(len(calls))
    class BrokenRedis:
        def hgetall(self, _key):
            raise redis.ConnectionError("synthetic unavailable")
        def eval(self, *_args):
            raise redis.ConnectionError("synthetic unavailable")
        def close(self):
            pass
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        current = provider(http)
        record = current.get_record()
        monkeypatch.setattr(get_settings(), "redis_url", "redis://127.0.0.1:1/0")
        monkeypatch.setattr(get_settings(), "master_key", SYNTHETIC_MASTER)
        monkeypatch.setattr(redis.Redis, "from_url", lambda *_a, **_kw: BrokenRedis())
        current.reject(record.token, record.generation)
        with pytest.raises(HcsError) as caught:
            current.get()
        assert caught.value.code == "IAM.CACHE_UNAVAILABLE" and calls == [1]


def test_token_state_repr_and_logs_do_not_expose_tokens_passwords_or_cache_keys(caplog):
    caplog.set_level(logging.DEBUG)
    with httpx.Client(transport=httpx.MockTransport(lambda _r: issued_response(1))) as http:
        current = provider(http)
        record = current.get_record()
        state = current._state(current._scope())
    output = "\n".join(JsonFormatter().format(r) for r in caplog.records if r.name.startswith("app."))
    assert record.token not in repr(record) + repr(state) + output
    assert "synthetic-password" not in output and current._scope() not in output


def test_public_cache_identity_is_stable_without_issuing_token_and_changes_with_credentials():
    with httpx.Client(transport=httpx.MockTransport(lambda _r: pytest.fail("identity must not authenticate"))) as http:
        first = provider(http)
        identity = first.cache_identity()
        assert len(identity) == 64 and identity == provider(http).cache_identity()
        assert identity != provider(http, password="synthetic-changed").cache_identity()
        assert identity != provider(http, endpoint_scope="endpoint-changed").cache_identity()
        assert first._token is None


def test_ces_is_blocked_before_authentication_unless_explicitly_enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", False)
    class Auth:
        kind = "TEST"
        def apply(self, _request, _service):
            pytest.fail("disabled CES must not authenticate")
        def refresh(self):
            pytest.fail("disabled CES must not refresh")
    with httpx.Client(transport=httpx.MockTransport(lambda _r: pytest.fail("disabled CES must not send"))) as http:
        current = HcsClient(http, Auth(), EndpointResolver({}, "r", "invalid"), "project")
        for method, path in (("GET", "/V1.0/{project_id}/metrics"),
                             ("POST", "/V1.0/{project_id}/batch-query-metric-data")):
            with pytest.raises(HcsError) as caught:
                current.request("ces", method, path)
            assert caught.value.code == "LOCAL.CES_DISABLED"
        assert current.calls == 0


@pytest.mark.parametrize("service,path", [
        ("gaussdb", "/gaussdb/v3/{project_id}/instances/instance-a/slow-sql-list"),
        ("ces", "/V1.0/{project_id}/batch-query-metric-data")])
def test_native_queries_and_explicit_legacy_ces_use_the_same_cached_iam(monkeypatch, service, path):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", service == "ces")
    tokens, requests = [], []
    def respond(request):
        if request.url.path == "/v3/auth/tokens":
            tokens.append(1)
            return issued_response(len(tokens), ttl=30)
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={"ok": True})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        current = HcsClient(http, TokenAuth(provider(http)),
                            EndpointResolver({service: "https://cloud.invalid"}, "r", "invalid"), "1001",
                            tenant_id=101, project_ref=1001, qps={service: 1000}, ces_enabled=service == "ces")
        assert current.post(service, path, json={"synthetic": "query"}) == {"ok": True}
        assert current.post(service, path, json={"synthetic": "query"}) == {"ok": True}
        with pytest.raises(HcsError, match="只允许"):
            current.post(service, "/v2/{project_id}/instances/delete", json={})
    assert len(tokens) == 1 and len(requests) == 2 and all(method == "POST" for method, _ in requests)


@pytest.mark.parametrize("path", ["/v1/{project_id}/ams/metrics", "/v2/{project_id}/series", "/v2/{project_id}/samples"])
def test_disabled_aom_does_not_acquire_or_use_iam_token(path):
    requests = []
    def respond(request):
        requests.append(request.url.path)
        pytest.fail("Disabled AOM must not reach IAM or the cloud")
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        token_provider = provider(http)
        current = HcsClient(http, TokenAuth(token_provider),
            EndpointResolver({"aom": "https://cloud.invalid"}, "r", "invalid"), "project",
            tenant_id=101, project_ref=1001)
        with pytest.raises(HcsError) as caught:
            current.post("aom", path, json={"old_mapping": True})
        assert caught.value.code == "LOCAL.AOM_DISABLED"
        assert token_provider._token is None and current.calls == 0 and requests == []


def test_disabled_aom_has_no_default_qps_or_mock_endpoint():
    from app.hcs.factory import DEFAULT_QPS, mock_endpoints

    assert "aom" not in DEFAULT_QPS
    assert "aom" not in mock_endpoints("http://mock/")
    assert "aom" not in get_settings().hcs_endpoints


def redis_token_key(current):
    version = hashlib.sha256((current._scope() + ":" + SYNTHETIC_MASTER).encode()).hexdigest()
    return f"inspector:iam:{{{version}}}:value"


def test_redis_record_is_encrypted_and_expires_at_real_iam_expiry(shared_redis, monkeypatch):
    url, seed, backend, _limiter = shared_redis
    monkeypatch.setattr(get_settings(), "redis_url", url)
    monkeypatch.setattr(get_settings(), "master_key", SYNTHETIC_MASTER)
    with httpx.Client(transport=httpx.MockTransport(lambda _r: issued_response(1, ttl=30))) as http:
        current = provider(http, password="synthetic-" + seed)
        record = current.get_record()
        key = redis_token_key(current)
        values = backend.hgetall(key)
        assert values[b"value"].decode().startswith(PREFIX)
        assert record.token.encode() not in b" ".join(values.values())
        assert current._password.encode() not in key.encode() + b" ".join(values.values())
        payload = json.loads(decrypt(values[b"value"].decode()))
        assert payload["expires_at"] == record.expires_at and payload["generation"] == record.generation
        assert 0 < backend.pttl(key) <= int((record.expires_at - time.time()) * 1000) + 100
        monkeypatch.setattr(auth_module, "_tokens", OrderedDict())
        with httpx.Client(transport=httpx.MockTransport(lambda _r: pytest.fail("unexpected second IAM"))) as next_http:
            assert provider(next_http, password="synthetic-" + seed).get() == record.token


def test_plaintext_or_wrong_scope_redis_payload_is_never_trusted(shared_redis, monkeypatch):
    url, seed, backend, _limiter = shared_redis
    monkeypatch.setattr(get_settings(), "redis_url", url)
    monkeypatch.setattr(get_settings(), "master_key", SYNTHETIC_MASTER)
    calls = []
    def respond(_request):
        calls.append(1)
        return issued_response(len(calls))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        current = provider(http, password="synthetic-" + seed)
        current.get()
        key = redis_token_key(current)
        backend.hset(key, "value", "synthetic-plaintext-token")
        monkeypatch.setattr(auth_module, "_tokens", OrderedDict())
        with pytest.raises(HcsError) as caught:
            provider(http, password="synthetic-" + seed).get()
        assert caught.value.code == "IAM.CACHE_INVALID" and len(calls) == 1
        foreign = provider(http, password="synthetic-foreign-" + seed)
        foreign.get()
        foreign_key = redis_token_key(foreign)
        backend.hset(key, mapping=backend.hgetall(foreign_key))
        monkeypatch.setattr(auth_module, "_tokens", OrderedDict())
        with pytest.raises(HcsError) as swapped:
            provider(http, password="synthetic-" + seed).get()
        assert swapped.value.code == "IAM.CACHE_INVALID" and len(calls) == 2


def test_rejecting_a_shared_generation_does_not_authenticate_or_delete_newer_record(shared_redis, monkeypatch):
    url, seed, backend, _limiter = shared_redis
    monkeypatch.setattr(get_settings(), "redis_url", url)
    monkeypatch.setattr(get_settings(), "master_key", SYNTHETIC_MASTER)
    calls = []
    def respond(_request):
        calls.append(1)
        return issued_response(len(calls))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        current = provider(http, password="synthetic-" + seed)
        first = current.get_record()
        key = redis_token_key(current)
        current.reject(first.token, first.generation)
        assert not backend.exists(key) and len(calls) == 1
        other = provider(http, password="synthetic-" + seed)
        second = other.get_record()
        assert second.generation != first.generation and len(calls) == 2
        current.reject(first.token, first.generation)
        assert backend.hget(key, "generation").decode() == second.generation
        assert current.get() == second.token and len(calls) == 2


def _token_worker(url, seed, ready, output, rejected=None, failure=False):
    settings = get_settings()
    settings.redis_url, settings.master_key, settings.hcs_total_qps = url, SYNTHETIC_MASTER, 1000
    backend = redis.Redis.from_url(url)
    def respond(_request):
        serial = backend.incr("test-iam-requests:" + seed)
        time.sleep(0.04)
        return httpx.Response(401, json={"error_code": "IAM.0101"}) if failure else issued_response(serial)
    try:
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            assert ready.wait(10)
            current = provider(http, password="synthetic-" + seed)
            if rejected is None:
                result = current.get_record(force=True)
            else:
                result = current.get_record(force=True, refresh=True,
                                             rejected_token=rejected[0], rejected_generation=rejected[1])
            output.put(("ok", result.token, result.generation))
    except HcsError as error:
        output.put(("error", error.code))
    finally:
        backend.close()


def token_workers(url, seed, *, rejected=None, failure=False):
    context = multiprocessing.get_context("spawn")
    ready, output = context.Event(), context.Queue()
    children = [context.Process(target=_token_worker, args=(url, seed, ready, output, rejected, failure)) for _ in range(3)]
    try:
        for child in children:
            child.start()
        ready.set()
        for child in children:
            child.join(15)
            assert child.exitcode == 0
        return [output.get(timeout=2) for _ in children]
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(2)
        output.close()


def test_processes_singleflight_force_checks_and_401_refresh(shared_redis, monkeypatch):
    url, seed, backend, _limiter = shared_redis
    first = token_workers(url, seed)
    assert len(set(first)) == 1 and first[0][0] == "ok"
    assert int(backend.get("test-iam-requests:" + seed)) == 1
    second = token_workers(url, seed, rejected=(first[0][1], first[0][2]))
    assert len(set(second)) == 1 and second[0][0] == "ok" and second[0][2] != first[0][2]
    assert int(backend.get("test-iam-requests:" + seed)) == 2
    monkeypatch.setattr(get_settings(), "redis_url", url)
    monkeypatch.setattr(get_settings(), "master_key", SYNTHETIC_MASTER)
    with httpx.Client(transport=httpx.MockTransport(lambda _r: pytest.fail("late 401 must not issue again"))) as http:
        current = provider(http, password="synthetic-" + seed)
        assert current.get(force=True, refresh=True, rejected_token=first[0][1],
                           rejected_generation=first[0][2]) == second[0][1]
    assert int(backend.get("test-iam-requests:" + seed)) == 2
    backend.delete("test-iam-requests:" + seed)


def test_processes_share_short_negative_authentication_cache(shared_redis):
    url, seed, backend, _limiter = shared_redis
    results = token_workers(url, seed, failure=True)
    assert results == [("error", "IAM.0101")] * 3
    assert int(backend.get("test-iam-requests:" + seed)) == 1
    backend.delete("test-iam-requests:" + seed)
