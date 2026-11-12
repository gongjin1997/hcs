"""告警事件推送：API 进程内广播给 SSE 订阅者；配置 Redis 时经 Redis Pub/Sub 跨进程转发。"""
import asyncio
import json
import logging
from contextlib import AsyncExitStack
from threading import Lock
from time import monotonic
from typing import TYPE_CHECKING

from .config import get_settings

if TYPE_CHECKING:
    import redis

CHANNEL = "inspector:alarms"
FORWARDER_RETRY_SEC = 5
log = logging.getLogger(__name__)
_publish_guard = Lock()
_publish_failures = 0
_publish_warned_at = 0.0
# 发布端共用的客户端及其地址：redis-py 的连接池线程安全，不必每个事件新建一次连接。
_publisher: "tuple[str, redis.Redis] | None" = None


class Broadcaster:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dropped = 0
        self._drop_warned_at: float | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    @property
    def count(self) -> int:
        return len(self._subs)

    def _deliver(self, event: dict) -> None:
        dropped = 0
        for q in list(self._subs):
            if not q.full():
                q.put_nowait(event)
            else:
                dropped += 1
        if dropped:
            self._dropped += dropped
            now = monotonic()
            if self._drop_warned_at is None or now - self._drop_warned_at >= 60:
                self._drop_warned_at = now
                log.warning("event_subscriber_overflow", extra={"event": "event_subscriber_overflow",
                            "dropped_count": self._dropped, "subscriber_count": len(self._subs)})

    def publish_threadsafe(self, event: dict) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._deliver, event)


broadcaster = Broadcaster()


def _publish_client(url: str) -> "redis.Redis":
    global _publisher
    with _publish_guard:
        if _publisher is None or _publisher[0] != url:
            import redis

            stale, _publisher = _publisher, (url, redis.Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5))
            if stale is not None:
                _close_client(stale[1])
        return _publisher[1]


def _discard_client(client: "redis.Redis") -> None:
    """发布失败后丢弃缓存的客户端，下次发布时重建。"""
    global _publisher
    with _publish_guard:
        if _publisher is not None and _publisher[1] is client:
            _publisher = None
    _close_client(client)


def _close_client(client: "redis.Redis") -> None:
    try:
        client.close()
    except Exception as exc:
        log.debug("event_publish_connection_close_failed", extra={
            "event": "event_publish_connection_close_failed", "exception_type": type(exc).__name__})


def publish(event: dict) -> None:
    """供业务代码调用（可在线程池 / Worker 进程中调用）。"""
    global _publish_failures, _publish_warned_at
    url = get_settings().redis_url
    if url:
        client = None
        try:
            payload = json.dumps(event, ensure_ascii=False, default=str)
            client = _publish_client(url)
            client.publish(CHANNEL, payload)
            with _publish_guard:
                previous, _publish_failures = _publish_failures, 0
            if previous:
                log.info("event_publish_recovered", extra={"event": "event_publish_recovered",
                                                          "previous_failure_count": previous})
            return
        except Exception as exc:  # Redis 不可用时退化为进程内广播；同一故障最多每分钟记录一次。
            if client is not None:
                _discard_client(client)
            now = monotonic()
            with _publish_guard:
                _publish_failures += 1
                warn = _publish_failures == 1 or now - _publish_warned_at >= 60
                count = _publish_failures
                if warn:
                    _publish_warned_at = now
            if warn:
                log.warning("event_publish_fallback", extra={"event": "event_publish_fallback", "failure_count": count,
                            "exception_type": type(exc).__name__, "fallback": "in_process"},
                            exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
    broadcaster.publish_threadsafe(event)


async def redis_forwarder() -> None:
    """API 进程后台任务：订阅 Redis 频道并转发给本进程的 SSE 订阅者。"""
    url = get_settings().redis_url
    if not url:
        return
    import redis.asyncio as aioredis

    failures, warned_at = 0, None
    # 转发任务全程共用一个客户端（连接池）；断线后只重建订阅，不再每次重连都新建客户端。
    async with AsyncExitStack() as stack:
        client = None
        while True:
            try:
                if client is None:
                    client = await stack.enter_async_context(aioredis.Redis.from_url(url))
                async with client.pubsub() as pubsub:
                    await pubsub.subscribe(CHANNEL)
                    log.info("event_forwarder_connected", extra={"event": "event_forwarder_connected",
                                                                "previous_failure_count": failures})
                    async for msg in pubsub.listen():
                        if msg.get("type") == "message":
                            broadcaster._deliver(json.loads(msg["data"]))
                    raise ConnectionError("event subscription ended")
            except asyncio.CancelledError:
                log.info("event_forwarder_stopped", extra={"event": "event_forwarder_stopped"})
                raise
            except Exception as exc:
                failures += 1
                now = monotonic()
                if warned_at is None or now - warned_at >= 60:
                    warned_at = now
                    log.warning("event_forwarder_failed", extra={"event": "event_forwarder_failed",
                                "failure_count": failures, "retry_after_sec": FORWARDER_RETRY_SEC,
                                "exception_type": type(exc).__name__},
                                exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
                await asyncio.sleep(FORWARDER_RETRY_SEC)
