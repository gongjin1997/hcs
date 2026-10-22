"""告警事件推送：API 进程内广播给 SSE 订阅者；配置 Redis 时经 Redis Pub/Sub 跨进程转发。"""
import asyncio
import json
import logging

from .config import get_settings

CHANNEL = "inspector:alarms"
log = logging.getLogger(__name__)


class Broadcaster:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

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
        for q in list(self._subs):
            if not q.full():
                q.put_nowait(event)

    def publish_threadsafe(self, event: dict) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._deliver, event)


broadcaster = Broadcaster()


def publish(event: dict) -> None:
    """供业务代码调用（可在线程池 / Worker 进程中调用）。"""
    url = get_settings().redis_url
    if url:
        try:
            import redis

            redis.Redis.from_url(url).publish(CHANNEL, json.dumps(event, ensure_ascii=False, default=str))
            return
        except Exception:  # Redis 不可用时退化为进程内广播
            log.exception("publish to redis failed")
    broadcaster.publish_threadsafe(event)


async def redis_forwarder() -> None:
    """API 进程后台任务：订阅 Redis 频道并转发给本进程的 SSE 订阅者。"""
    url = get_settings().redis_url
    if not url:
        return
    import redis.asyncio as aioredis

    while True:
        try:
            client = aioredis.Redis.from_url(url)
            pubsub = client.pubsub()
            await pubsub.subscribe(CHANNEL)
            async for msg in pubsub.listen():
                if msg.get("type") == "message":
                    broadcaster._deliver(json.loads(msg["data"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("redis forwarder error, retry in 5s")
            await asyncio.sleep(5)
