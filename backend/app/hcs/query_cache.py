"""短时查询缓存和 singleflight；键由调用方隔离 scope/config，Redis 故障不重复打云。"""
import json
import threading
import time
import uuid
import zlib
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

import redis

from app.core.config import get_settings

from .errors import HcsError
from .ratelimit import remaining_time

_UNLOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
_PUBLISH = """
local tm = redis.call('TIME')
local now = tonumber(tm[1]) * 1000 + tonumber(tm[2]) / 1000
local total = tonumber(redis.call('HGET', KEYS[3], '_bytes') or '0')
local function remove(key)
  total = math.max(0, total - tonumber(redis.call('HGET', KEYS[3], key) or '0'))
  redis.call('DEL', key)
  redis.call('HDEL', KEYS[3], key)
  redis.call('ZREM', KEYS[2], key)
end
for _, key in ipairs(redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)) do remove(key) end
remove(KEYS[1])
local size = string.len(ARGV[1])
while total + size > tonumber(ARGV[3]) or redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[4]) do
  local oldest = redis.call('ZRANGE', KEYS[2], 0, 0)[1]
  if not oldest then return 0 end
  remove(oldest)
end
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[2]), KEYS[1])
redis.call('HSET', KEYS[3], KEYS[1], size, '_bytes', total + size)
redis.call('PEXPIRE', KEYS[2], 120000)
redis.call('PEXPIRE', KEYS[3], 120000)
return 1
"""


@dataclass
class _Entry:
    done: threading.Event = field(default_factory=threading.Event)
    expires: float = 0
    value: bytes | None = None


class QueryCache:
    MAX_BYTES = 32 * 1024 * 1024
    MAX_CACHE_BYTES = 64 * 1024 * 1024

    def __init__(self) -> None:
        self.entries: OrderedDict[tuple[str | None, str], _Entry] = OrderedDict()
        self.lock = threading.Lock()
        self.bytes = 0

    def _remove(self, key: tuple[str | None, str]) -> None:
        entry = self.entries.pop(key)
        self.bytes -= len(entry.value) if entry.value else 0

    @staticmethod
    def _decode(value: bytes) -> dict:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(value, QueryCache.MAX_BYTES)
        if not decoder.eof or decoder.unconsumed_tail:
            raise ValueError("invalid cached query")
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("invalid cached query")
        return body

    def get(self, key: str, load: Callable[[], dict], *, ttl: int = 30) -> tuple[dict, str]:
        url = get_settings().redis_url
        local_key = (url, key)
        with self.lock:
            entry = self.entries.get(local_key)
            if entry is not None and entry.done.is_set() and entry.expires <= time.monotonic():
                self._remove(local_key)
                entry = None
            leader = entry is None
            if entry is None:
                # 只淘汰已完成条目，不能移除正在合并的查询而产生第二个 leader。
                while len(self.entries) >= 64:
                    victim = next((k for k, v in self.entries.items() if v.done.is_set()), None)
                    if victim is None:
                        raise HcsError(0, "LOCAL.QUERY_BUSY", "历史查询繁忙，请稍后重试")
                    self._remove(victim)
                entry = self.entries[local_key] = _Entry()
            self.entries.move_to_end(local_key)
        if not leader:
            if not entry.done.wait(remaining_time(65)) or entry.value is None:
                raise HcsError(0, "LOCAL.QUERY_BUSY", "相同历史查询正在处理，请稍后重试")
            return self._decode(entry.value), "hit"
        try:
            value, status = self._load(key, load, url, ttl)
            body = self._decode(value)
            with self.lock:
                while self.bytes + len(value) > self.MAX_CACHE_BYTES:
                    victim = next((k for k, v in self.entries.items() if k != local_key and v.done.is_set()), None)
                    if victim is None:
                        raise HcsError(0, "LOCAL.QUERY_BUSY", "历史查询结果超过缓存总容量，请缩小范围")
                    self._remove(victim)
                entry.value = value
                self.bytes += len(value)
                entry.expires = time.monotonic() + (2 if body.get("failed") else ttl)
                entry.done.set()
            return body, status
        finally:
            # 失败不让等待者重新成为 leader；它们得到安全错误而不是重复请求。
            entry.done.set()

    def _load(self, key: str, load: Callable[[], dict], url: str | None, ttl: int) -> tuple[bytes, str]:
        backend = redis.Redis.from_url(url, socket_connect_timeout=0.25, socket_timeout=0.25) if url else None
        prefix = "inspector:history:{cache}:"
        data_key, lock_key, token = prefix + "data:" + key, prefix + "lock:" + key, uuid.uuid4().hex
        acquired = False
        try:
            if backend is not None:
                deadline = time.monotonic() + remaining_time(65)
                while True:
                    found = backend.get(data_key)
                    if found is not None:
                        if not isinstance(found, bytes):
                            raise ValueError("invalid cached query")
                        self._decode(found)
                        return found, "shared_hit"
                    if backend.set(lock_key, token, nx=True, ex=120):
                        acquired = True
                        # leader 交接之间可能刚写好缓存；二次读取避免重复请求。
                        found = backend.get(data_key)
                        if found is not None:
                            if not isinstance(found, bytes):
                                raise ValueError("invalid cached query")
                            self._decode(found)
                            return found, "shared_hit"
                        break
                    if time.monotonic() >= deadline:
                        raise HcsError(0, "LOCAL.QUERY_BUSY", "相同历史查询正在处理，请稍后重试")
                    time.sleep(0.05)
            body = load()
            raw = json.dumps(body, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
            if len(raw) > self.MAX_BYTES:
                raise HcsError(0, "LOCAL.RESPONSE_BUDGET", "历史查询结果超过缓存预算，请缩小时间范围")
            value = zlib.compress(raw)
            if backend is not None:
                backend.eval(_PUBLISH, 3, data_key, prefix + "entries", prefix + "sizes", value,
                             (2 if body.get("failed") else ttl) * 1000, self.MAX_CACHE_BYTES, 128)
            return value, "miss"
        except (redis.RedisError, ValueError, zlib.error):
            raise HcsError(0, "LOCAL.QUERY_CACHE", "共享历史查询暂不可用，请稍后重试") from None
        finally:
            if backend is not None:
                if acquired:
                    try:
                        backend.eval(_UNLOCK, 1, lock_key, token)
                    except redis.RedisError:
                        pass
                backend.close()


history_cache = QueryCache()
