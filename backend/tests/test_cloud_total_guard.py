"""跨服务总闸：本地 MockTransport；可选独立 Redis 的真实跨进程回归。"""
import multiprocessing
import threading
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.core.config import get_settings
from app.hcs import auth as auth_module
from app.hcs import ratelimit
from app.hcs.auth import AkSkAuth, TokenAuth, TokenProvider
from app.hcs.client import HcsClient
from app.hcs.endpoints import EndpointResolver
from app.hcs.errors import HcsError
from app.hcs.ratelimit import acquire_cloud, request_budget, service_limiter, total_limiter
from app.metrics.prometheus import PromClient

from .test_cloud_shared_redis import shared_redis as shared_redis


@pytest.fixture(autouse=True)
def isolated_total(monkeypatch):
    monkeypatch.setattr(ratelimit, "_shared", {})
    monkeypatch.setattr(auth_module, "_tokens", OrderedDict())
    monkeypatch.setattr(get_settings(), "redis_url", None)
    monkeypatch.setattr(get_settings(), "hcs_service_concurrency", 2)
    monkeypatch.setattr(get_settings(), "hcs_total_qps", 10)
    monkeypatch.setattr(get_settings(), "hcs_total_concurrency", 4)


def hcs(http, auth=None):
    return HcsClient(http, auth or AkSkAuth("synthetic-ak", "synthetic-sk"),
                     EndpointResolver({s: "https://cloud.invalid" for s in ("dcs", "ecs", "dws")}, "test", "invalid"),
                     "project", qps={"dcs": 1000, "ecs": 1000, "dws": 1000}, max_retries=0)


def test_two_services_share_default_ten_qps_without_initial_burst():
    starts = []
    def respond(_request):
        starts.append(time.monotonic())
        return httpx.Response(200, json={})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http, ThreadPoolExecutor(max_workers=8) as pool:
        current = hcs(http)
        list(pool.map(lambda i: current.get("dcs" if i % 2 else "ecs", "/instances"), range(8)))
    starts.sort()
    assert len(starts) == 8 and starts[-1] - starts[0] >= 0.65
    assert all(b - a >= 0.085 for a, b in zip(starts, starts[1:], strict=False))


def test_three_services_share_four_inflight_and_keep_two_per_service(monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_total_qps", 1000)
    active, peak, per_service, peaks = 0, 0, Counter(), Counter()
    lock = threading.Lock()
    def respond(request):
        nonlocal active, peak
        service = request.url.path.strip("/")
        with lock:
            active += 1
            peak = max(peak, active)
            per_service[service] += 1
            peaks[service] = max(peaks[service], per_service[service])
        time.sleep(0.06)
        with lock:
            active -= 1
            per_service[service] -= 1
        return httpx.Response(200, json={})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http, ThreadPoolExecutor(max_workers=12) as pool:
        current = hcs(http)
        services = ["dcs", "ecs", "dws"] * 4
        list(pool.map(lambda s: current.get(s, "/" + s), services))
    assert active == 0 and peak == 4 and all(v <= 2 for v in peaks.values())
    assert not total_limiter()._active and all(not service_limiter(s)._active for s in services)


def test_iam_hcs_and_prometheus_all_count_toward_the_same_pacing():
    starts = []
    def respond(request):
        starts.append((request.url.path, time.monotonic()))
        if request.url.path == "/v3/auth/tokens":
            return httpx.Response(201, headers={"X-Subject-Token": "synthetic-test-token"},
                                  json={"token": {"expires_at": "2030-01-01T00:00:00Z"}})
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        provider = TokenProvider(http, "https://iam.invalid", "test", "reader", "synthetic-value", "project")
        current = hcs(http, TokenAuth(provider))
        current.get("dcs", "/instances")
        assert PromClient("https://prom.invalid", http).query("up") == []
    assert [path for path, _start in starts] == ["/v3/auth/tokens", "/instances", "/api/v1/query"]
    assert all(b[1] - a[1] >= 0.085 for a, b in zip(starts, starts[1:], strict=False))
    assert not total_limiter()._active


def test_auth_refresh_does_not_hold_business_or_total_lease(monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_total_concurrency", 1)
    monkeypatch.setattr(get_settings(), "hcs_service_concurrency", 1)
    tokens, business = [], []
    def respond(request):
        if request.url.path == "/v3/auth/tokens":
            tokens.append(1)
            return httpx.Response(201, headers={"X-Subject-Token": f"synthetic-token-{len(tokens)}"},
                                  json={"token": {"expires_at": "2030-01-01T00:00:00Z"}})
        business.append(1)
        return httpx.Response(401 if len(business) == 1 else 200, json={})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        provider = TokenProvider(http, "https://iam.invalid", "test", "reader", "synthetic-value", "project")
        current = hcs(http, TokenAuth(provider))
        current.max_retries = 1
        with request_budget(attempts=4, seconds=2):
            assert current.get("dcs", "/instances") == {}
    assert len(tokens) == len(business) == 2 and current.calls == 4
    assert not total_limiter()._active and not service_limiter("dcs")._active


def test_total_rejection_releases_service_lease_and_uses_one_deadline(monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_total_concurrency", 1)
    total = total_limiter()
    token = total.acquire(1000, 1)
    try:
        with pytest.raises(HcsError, match="等待超时"):
            acquire_cloud("dcs", 1000, 2, timeout=0.03)
        assert not service_limiter("dcs")._active
    finally:
        total.release(token)
    service_limiter("dcs").defer(0.04)
    total.defer(0.2)
    started = time.monotonic()
    with request_budget(seconds=0.06):
        with pytest.raises(HcsError):
            acquire_cloud("dcs", 1000, 2, timeout=1)
    assert time.monotonic() - started < 0.1
    assert not service_limiter("dcs")._active and not total._active


def test_network_failure_releases_both_leases():
    def fail(request):
        raise httpx.ConnectError("synthetic unavailable", request=request)
    with httpx.Client(transport=httpx.MockTransport(fail)) as http:
        with pytest.raises(HcsError):
            hcs(http).get("dcs", "/instances")
    assert not service_limiter("dcs")._active and not total_limiter()._active


def _total_worker(url, service, ready, output):
    settings = get_settings()
    settings.redis_url, settings.hcs_total_qps, settings.hcs_total_concurrency = url, 5, 4
    settings.hcs_service_concurrency = 2
    def request(_index):
        assert ready.wait(10)
        for _ in range(2):
            lease = acquire_cloud(service, 1000, 2, timeout=10)
            started = time.monotonic()
            try:
                time.sleep(0.7)
            finally:
                lease.release()
            output.put((started, time.monotonic()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(request, range(2)))


def test_redis_total_is_shared_across_processes_and_distinct_services(shared_redis):
    url, service, backend, _limiter = shared_redis
    context = multiprocessing.get_context("spawn")
    ready, output = context.Event(), context.Queue()
    children = [context.Process(target=_total_worker, args=(url, service + str(i), ready, output)) for i in range(2)]
    try:
        for child in children:
            child.start()
        ready.set()
        for child in children:
            child.join(15)
            assert child.exitcode == 0
        windows = sorted(output.get(timeout=2) for _ in range(8))
        assert windows[-1][0] - windows[0][0] >= 1.35
        assert all(b[0] - a[0] >= 0.17 for a, b in zip(windows, windows[1:], strict=False))
        changes = sorted([(start, 1) for start, _ in windows] + [(end, -1) for _, end in windows])
        active = peak = 0
        for _at, delta in changes:
            active += delta
            peak = max(peak, active)
        assert active == 0 and peak <= 4
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(2)
        output.close()
        for name in (service + "0", service + "1", "__total__"):
            backend.delete(f"inspector:cloud:{{{name}}}:pace", f"inspector:cloud:{{{name}}}:active")
