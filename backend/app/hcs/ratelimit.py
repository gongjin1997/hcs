import math
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import redis

from app.core.config import get_settings

from .errors import HcsError

# Redis TIME 避免各进程时钟偏移。平滑间隔与在途租约在同一原子操作中取得，无初始整桶突发。
_ACQUIRE = """
local tm = redis.call('TIME')
local now = tonumber(tm[1]) * 1000 + tonumber(tm[2]) / 1000
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local last = tonumber(redis.call('HGET', KEYS[1], 'last') or '0')
local interval = tonumber(redis.call('HGET', KEYS[1], 'interval') or '0')
if ARGV[5] == '1' then redis.call('HSET', KEYS[1], 'policy', ARGV[1]) end
local requested = tonumber(redis.call('HGET', KEYS[1], 'policy') or ARGV[1])
local cooldown = tonumber(redis.call('HGET', KEYS[1], 'cooldown') or '0')
local next_at = math.max(cooldown, last + math.max(interval, requested))
if next_at > now then return {0, math.ceil(next_at - now)} end
local limit = tonumber(ARGV[2])
for _, member in ipairs(redis.call('ZRANGE', KEYS[2], 0, -1)) do
  limit = math.min(limit, tonumber(string.match(member, '^(%d+):')) or limit)
end
if redis.call('ZCARD', KEYS[2]) >= limit then return {0, 25} end
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[3]), ARGV[4])
redis.call('HSET', KEYS[1], 'last', now, 'interval', requested)
redis.call('PEXPIRE', KEYS[1], math.max(60000, math.ceil(cooldown - now + 60000),
                                     math.ceil(math.max(interval, requested) + 60000)))
redis.call('PEXPIRE', KEYS[2], math.max(redis.call('PTTL', KEYS[2]), tonumber(ARGV[3]) + 1000))
return {1, 0}
"""
_DEFER = """
local tm = redis.call('TIME')
local now = tonumber(tm[1]) * 1000 + tonumber(tm[2]) / 1000
local until_at = math.max(tonumber(redis.call('HGET', KEYS[1], 'cooldown') or '0'), now + tonumber(ARGV[1]))
redis.call('HSET', KEYS[1], 'cooldown', until_at)
redis.call('PEXPIRE', KEYS[1], math.ceil(until_at - now + 60000))
return 1
"""


@dataclass
class RequestBudget:
    remaining: int
    deadline: float

    def consume(self) -> None:
        if self.remaining <= 0 or self.deadline <= time.monotonic():
            raise HcsError(0, "LOCAL.REQUEST_BUDGET", "云查询已达到本次请求或时间预算，请缩小范围后重试")
        self.remaining -= 1


_budget: ContextVar[RequestBudget | None] = ContextVar("cloud_request_budget", default=None)


@contextmanager
def request_budget(attempts: int = 384, seconds: float = 60):
    parent = _budget.get()
    current = RequestBudget(min(attempts, parent.remaining) if parent else attempts,
                            min(time.monotonic() + seconds, parent.deadline) if parent else time.monotonic() + seconds)
    initial = current.remaining
    token = _budget.set(current)
    try:
        yield
    finally:
        _budget.reset(token)
        if parent:
            parent.remaining -= initial - current.remaining


def consume_budget() -> None:
    budget = _budget.get()
    if budget:
        budget.consume()


def remaining_time(maximum: float) -> float:
    budget = _budget.get()
    return min(maximum, max(0, budget.deadline - time.monotonic())) if budget else maximum


class ServiceLimiter:
    """同一进程/Redis 中按服务共享；Redis 不可用时拒绝请求，不突发降级放行。"""

    LEASE_MS = 60000  # 大于工厂 HTTP 请求的 10 秒超时；进程退出后租约自动释放。

    def __init__(self, service: str, backend=None, *, total: bool = False):
        self.backend = backend
        self.total = total
        self.keys = (f"inspector:cloud:{{{service}}}:pace", f"inspector:cloud:{{{service}}}:active")
        self._condition = threading.Condition()
        self._last, self._interval, self._cooldown = 0.0, 0.0, 0.0
        self._policy_interval: float | None = None
        self._active: dict[str, int] = {}

    def acquire(self, rate: float, concurrency: int, timeout: float = 30, *, configured: bool = False) -> str:
        if not math.isfinite(rate) or rate <= 0 or concurrency < 1:
            raise ValueError("QPS 和并发数须为有限正数")
        settings = get_settings()
        ceiling = getattr(settings, "hcs_total_concurrency", 4) if self.total else settings.hcs_service_concurrency
        concurrency = min(concurrency, ceiling)
        deadline = time.monotonic() + remaining_time(timeout)
        # 服务租约取得后还可能等待总闸；总闸租约只需覆盖实际在途请求。
        lease_ms = self.LEASE_MS if self.total else self.LEASE_MS + math.ceil(remaining_time(timeout) * 1000)
        token, interval = f"{concurrency}:" + uuid.uuid4().hex, 1 / rate
        while True:
            if time.monotonic() >= deadline:
                raise HcsError(0, "LOCAL.RATE_LIMIT", "云请求等待预算已用尽，未向上游发送请求")
            if self.backend is not None:
                try:
                    acquired, wait_ms = self.backend.eval(_ACQUIRE, 2, *self.keys, interval * 1000,
                                                          concurrency, lease_ms, token, int(configured))
                except redis.RedisError:
                    raise HcsError(0, "LOCAL.CLOUD_GUARD", "共享云请求限速暂不可用，未向上游发送请求") from None
                if acquired:
                    return token
                wait = max(0.001, float(wait_ms) / 1000)
            else:
                with self._condition:
                    if configured:
                        self._policy_interval = interval
                    effective = self._policy_interval or interval
                    now = time.monotonic()
                    wait = max(0, self._cooldown - now, self._last + max(effective, self._interval) - now)
                    ceiling = min(concurrency, min(self._active.values(), default=concurrency))
                    if wait == 0 and len(self._active) < ceiling:
                        self._last, self._interval = now, effective
                        self._active[token] = concurrency
                        return token
                    wait = wait or 0.025
            left = deadline - time.monotonic()
            if left <= 0 or wait > left:
                raise HcsError(0, "LOCAL.RATE_LIMIT", "云请求限速或并发等待超时，未向上游发送请求")
            time.sleep(min(wait, 0.1))

    def release(self, token: str) -> None:
        if self.backend is not None:
            try:
                self.backend.zrem(self.keys[1], token)
            except redis.RedisError:
                pass  # 仅释放失败；保留的租约会限制后续流量，不会额外放行。
        else:
            with self._condition:
                self._active.pop(token, None)
                self._condition.notify_all()

    def defer(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds <= 0:
            return
        if self.backend is not None:
            try:
                self.backend.eval(_DEFER, 1, self.keys[0], seconds * 1000)
            except redis.RedisError:
                raise HcsError(0, "LOCAL.CLOUD_GUARD", "共享云请求退避暂不可用，已停止重试") from None
        else:
            with self._condition:
                self._cooldown = max(self._cooldown, time.monotonic() + seconds)


_shared: dict[tuple[str | None, str], ServiceLimiter] = {}
_shared_lock = threading.Lock()


def _shared_limiter(service: str, *, total: bool = False) -> ServiceLimiter:
    url = get_settings().redis_url
    with _shared_lock:
        key = (url, service)
        if key not in _shared:
            backend = redis.Redis.from_url(url, socket_connect_timeout=0.25, socket_timeout=0.25,
                                           decode_responses=True) if url else None
            _shared[key] = ServiceLimiter(service, backend, total=total)
        return _shared[key]


def service_limiter(service: str) -> ServiceLimiter:
    return _shared_limiter(service)


def total_limiter() -> ServiceLimiter:
    """独立总闸；不经 service_limiter 递归，也不受每服务 2 并发夹制。"""
    return _shared_limiter("__total__", total=True)


@dataclass(frozen=True)
class CloudLease:
    service: ServiceLimiter
    service_token: str
    total: ServiceLimiter
    total_token: str

    def release(self) -> None:
        try:
            self.total.release(self.total_token)
        finally:
            self.service.release(self.service_token)


def acquire_cloud(service: str, rate: float, concurrency: int, timeout: float = 30, *,
                  configured: bool = False) -> CloudLease:
    """认证完成后先取服务租约再取总租约；两层共用一个等待/请求截止时间。"""
    if service == "__total__":
        raise ValueError("保留的内部限速标识")
    deadline = time.monotonic() + remaining_time(timeout)
    scoped = service_limiter(service)
    service_token = scoped.acquire(rate, concurrency, max(0, deadline - time.monotonic()), configured=configured)
    try:
        settings = get_settings()
        total = total_limiter()
        total_token = total.acquire(getattr(settings, "hcs_total_qps", 10),
                                    getattr(settings, "hcs_total_concurrency", 4),
                                    max(0, deadline - time.monotonic()), configured=True)
        if time.monotonic() >= deadline:
            total.release(total_token)
            raise HcsError(0, "LOCAL.RATE_LIMIT", "云请求等待预算已用尽，未向上游发送请求")
        return CloudLease(scoped, service_token, total, total_token)
    except BaseException:
        scoped.release(service_token)
        raise


class TokenBucket:
    """令牌桶限流：按服务维度控制调用 HCS API 的 QPS，避免巡检流量冲击 API 网关。"""

    def __init__(self, rate: float, capacity: int | None = None):
        self.rate = rate
        self.capacity = capacity or max(1, int(rate))
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return True
                wait = (1 - self._tokens) / self.rate
            if time.monotonic() + wait > deadline:
                return False
            time.sleep(wait)
