"""可选的真实 Redis 跨进程验证；仅使用显式测试地址和独立随机键，不访问云。

INSPECTOR_TEST_REDIS_URL 应指向短命专用测试 Redis。没有配置时跳过。
"""
import multiprocessing
import os
import socket
import time
import uuid
from urllib.parse import urlsplit

import httpx
import pytest
import redis

from app.core.config import get_settings
from app.hcs.auth import AkSkAuth
from app.hcs.client import HcsClient
from app.hcs.endpoints import EndpointResolver
from app.hcs.errors import HcsError
from app.hcs.ratelimit import ServiceLimiter


@pytest.fixture
def shared_redis():
    url = os.environ.get("INSPECTOR_TEST_REDIS_URL")
    if not url:
        pytest.skip("需要显式短命测试 Redis，INSPECTOR_TEST_REDIS_URL")
    parsed = urlsplit(url)
    assert parsed.hostname in ("127.0.0.1", "localhost", "::1"), "仅使用本机隔离测试 Redis"
    backend = redis.Redis.from_url(url, socket_connect_timeout=0.5, socket_timeout=0.5)
    assert backend.ping()
    service = f"integration_{uuid.uuid4().hex}"
    limiter = ServiceLimiter(service, backend)
    try:
        yield url, service, backend, limiter
    finally:
        backend.delete(*limiter.keys)
        backend.close()


def _requests(url, service, ready, output):
    backend = redis.Redis.from_url(url, socket_connect_timeout=0.5, socket_timeout=0.5)
    limiter = ServiceLimiter(service, backend)
    try:
        assert ready.wait(10)
        for _ in range(4):
            token = limiter.acquire(4, 2, timeout=10)
            start = time.monotonic()
            try:
                time.sleep(0.3)
            finally:
                limiter.release(token)
            output.put((start, time.monotonic()))
    finally:
        backend.close()


def test_shared_pacing_and_concurrency_across_real_processes(shared_redis):
    url, service, _, _ = shared_redis
    context = multiprocessing.get_context("spawn")
    ready, output = context.Event(), context.Queue()
    children = [context.Process(target=_requests, args=(url, service, ready, output)) for _ in range(2)]
    try:
        for child in children:
            child.start()
        ready.set()
        for child in children:
            child.join(15)
            assert child.exitcode == 0
        windows = sorted(output.get(timeout=2) for _ in range(8))
        gaps = [b[0] - a[0] for a, b in zip(windows, windows[1:], strict=False)]
        assert min(gaps) >= 0.21  # 4 QPS 的 250ms 间隔，允许进程调度/计时误差。
        assert windows[-1][0] - windows[0][0] >= 1.7
        changes = sorted([(start, 1) for start, _ in windows] + [(end, -1) for _, end in windows])
        active, peak = 0, 0
        for _, delta in changes:
            active += delta
            peak = max(active, peak)
        assert active == 0 and peak <= 2
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(2)
        output.close()


def test_shared_cooldown_and_hard_concurrency_limit(shared_redis, monkeypatch):
    _, service, backend, limiter = shared_redis
    monkeypatch.setattr(get_settings(), "hcs_service_concurrency", 2)
    other = ServiceLimiter(service, backend)
    tokens = []
    try:
        tokens = [limiter.acquire(1000, 16), other.acquire(1000, 16)]
        with pytest.raises(HcsError, match="等待超时"):
            other.acquire(1000, 16, timeout=0.1)
    finally:
        for token in tokens:
            limiter.release(token)
    limiter.defer(0.4)
    start = time.monotonic()
    token = other.acquire(1000, 2, timeout=1)
    assert time.monotonic() - start >= 0.35
    other.release(token)


def test_redis_unavailable_never_sends_cloud_request(monkeypatch):
    with socket.socket() as temporary:
        temporary.bind(("127.0.0.1", 0))
        port = temporary.getsockname()[1]
    monkeypatch.setattr(get_settings(), "redis_url", f"redis://127.0.0.1:{port}/0")
    sent = []
    with httpx.Client(transport=httpx.MockTransport(lambda req: sent.append(req) or httpx.Response(200, json={}))) as http:
        client = HcsClient(http, AkSkAuth("synthetic-ak", "synthetic-sk"),
                           EndpointResolver({"ecs": "mock.invalid"}, "r", "example.invalid", "http"), "project")
        with pytest.raises(HcsError) as raised:
            client.get("ecs", "/v1/{project_id}/cloudservers/detail")
    assert raised.value.code == "LOCAL.CLOUD_GUARD" and sent == [] and client.calls == 0
