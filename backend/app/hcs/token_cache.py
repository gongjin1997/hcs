"""IAM Token 真实时效缓存：本地热路径、Redis 加密共享、按拒绝代次刷新。"""
import hashlib
import json
import logging
import math
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import redis

from app.core.config import get_settings
from app.core.crypto import PREFIX, decrypt, encrypt

from .errors import HcsError
from .ratelimit import remaining_time

log = logging.getLogger(__name__)
_UNLOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
_INVALIDATE = """
if redis.call('HGET', KEYS[1], 'generation') == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
_PUBLISH = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('HSET', KEYS[2], 'generation', ARGV[2], 'value', ARGV[3])
redis.call('PEXPIREAT', KEYS[2], ARGV[4])
return 1
"""


@dataclass(frozen=True, repr=False)
class TokenRecord:
    token: str
    expires_at: float
    refresh_at: float
    obtained_at: float
    generation: str
    checked_at: float = 0

    def valid(self, now: float) -> bool:
        return now < self.expires_at


@dataclass(repr=False)
class TokenState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    record: TokenRecord | None = None
    failure: tuple[int, str, str, float] | None = None
    retry_at: float = 0
    warned: bool = False
    users: int = 0
    rejected_generation: str | None = None
    rejected_token: str | None = None


def from_response(body: object, token: str | None, now: float) -> TokenRecord:
    if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,65536}", token):
        raise HcsError(201, "IAM.INVALID_TOKEN", "IAM 未返回有效 Token")
    try:
        item = body.get("token") if isinstance(body, dict) else None
        value = item.get("expires_at") if isinstance(item, dict) else None
        if not isinstance(value, str):
            raise ValueError("missing expiry")
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("missing timezone")
        expires = stamp.astimezone(UTC).timestamp()
        if not math.isfinite(expires) or expires <= now:
            raise ValueError("expired token")
    except (ValueError, TypeError, OverflowError):
        raise HcsError(201, "IAM.INVALID_EXPIRY", "IAM Token 有效期缺失、无时区、已过期或格式不正确") from None
    return TokenRecord(token, expires, expires - min(3600, (expires - now) * 0.1), now, uuid.uuid4().hex)


class TokenCache:
    FAILURE_SECONDS = 2
    FORCE_SECONDS = 2

    @staticmethod
    def reject_local(state: TokenState, token: str | None, generation: str | None) -> None:
        current = state.record
        matches = current is not None and (current.generation == generation if generation is not None
                                           else token is not None and current.token == token)
        if matches or current is None and state.rejected_generation is None and state.rejected_token is None:
            state.record = None
            state.rejected_generation, state.rejected_token = generation, token if generation is None else None

    @staticmethod
    def invalidate_shared(scope: str, generation: str | None) -> None:
        settings = get_settings()
        if not settings.redis_url or not settings.master_key or not generation:
            return
        version = hashlib.sha256((scope + ":" + settings.master_key).encode()).hexdigest()
        backend = None
        try:
            backend = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=0.25, socket_timeout=0.25)
            backend.eval(_INVALIDATE, 1, f"inspector:iam:{{{version}}}:value", generation)
        except (redis.RedisError, ValueError):
            log.warning("iam.cache.invalidate_failed", extra={"cloud_service": "iam", "error_code": "IAM.CACHE_WRITE"})
        finally:
            if backend is not None:
                try:
                    backend.close()
                except redis.RedisError:
                    pass

    @staticmethod
    def _accepted(record: TokenRecord | None, now: float, *, force: bool, refresh: bool,
                  rejected_generation: str | None, rejected_token: str | None, retry_at: float = 0) -> bool:
        if record is None or not record.valid(now):
            return False
        if rejected_generation is not None:
            if record.generation == rejected_generation:
                return False
        elif rejected_token is not None and record.token == rejected_token:
            return False
        if refresh:
            return True  # 新代次不得因迟到的旧 401 再次失效。
        if force:
            return record.checked_at > 0 and 0 <= now - record.checked_at < TokenCache.FORCE_SECONDS
        return now < record.refresh_at or now < retry_at

    def get(self, scope: str, state: TokenState, load: Callable[[], TokenRecord], *, force: bool = False,
            refresh: bool = False, rejected_generation: str | None = None, rejected_token: str | None = None,
            timeout: float = 30) -> TokenRecord:
        now = time.time()
        if rejected_generation is None and rejected_token is None:
            rejected_generation, rejected_token = state.rejected_generation, state.rejected_token
        options: dict[str, Any] = {"force": force, "refresh": refresh, "rejected_generation": rejected_generation,
                                   "rejected_token": rejected_token}
        if self._accepted(state.record, now, retry_at=state.retry_at, **options):
            assert state.record is not None
            return state.record
        if state.record is not None and ((rejected_generation is not None and
                state.record.generation == rejected_generation) or (rejected_generation is None and
                rejected_token is not None and state.record.token == rejected_token)):
            state.record = None
        if state.failure and now < state.failure[3]:
            if not force and state.record is not None and state.record.valid(now):
                return state.record
            status, code, message, _until = state.failure
            raise HcsError(status, code, message)
        try:
            if get_settings().redis_url:
                result = self._shared(scope, load, options, timeout)
            else:
                result = load()
                if force and not refresh:
                    result = replace(result, checked_at=time.time())
            if not result.valid(time.time()):
                raise HcsError(201, "IAM.INVALID_EXPIRY", "IAM Token 在缓存前已过期")
        except HcsError as error:
            state.failure = (error.status, error.code, error.message, time.time() + self.FAILURE_SECONDS)
            state.retry_at = time.time() + self.FAILURE_SECONDS
            if not state.warned:
                log.warning("iam.cache.refresh_failed", extra={"cloud_service": "iam", "error_code": error.code,
                            "status": error.status})
                state.warned = True
            if not force and state.record is not None and state.record.valid(time.time()):
                return state.record
            raise
        state.record, state.failure, state.retry_at, state.warned = result, None, 0, False
        state.rejected_generation, state.rejected_token = None, None
        return result

    @staticmethod
    def _read(backend, key: str, scope: str) -> TokenRecord | None:
        raw = backend.hgetall(key)
        if not raw:
            return None
        try:
            cipher = raw[b"value"].decode()
            if not cipher.startswith(PREFIX) or len(cipher) > 131072:
                raise ValueError("invalid encryption")
            body = json.loads(decrypt(cipher))
            if not isinstance(body, dict) or body.get("scope") != scope:
                raise ValueError("invalid scope")
            expiry = body["expires_at"]
            if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                raise ValueError("invalid expiry")
            if expiry <= time.time():
                return None
            if body.get("kind") == "failure":
                raise HcsError(int(body["status"]), str(body["code"]), "IAM 最近一次认证失败，请稍后重试")
            token, generation = body["token"], body["generation"]
            obtained, refresh, checked = body["obtained_at"], body["refresh_at"], body.get("checked_at", 0)
            if (not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,65536}", token)
                    or not isinstance(generation, str) or not re.fullmatch(r"[a-f0-9]{32}", generation)
                    or raw[b"generation"].decode() != generation
                    or any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t)
                           for t in (obtained, refresh, checked)) or not 0 < obtained <= refresh < expiry
                    or not 0 <= checked < expiry):
                raise ValueError("invalid record")
            return TokenRecord(token, expiry, refresh, obtained, generation, checked)
        except HcsError:
            raise
        except Exception:
            # 包括 InvalidTag；不回显密文、Token、配置摘要或解密异常的原文。
            raise HcsError(0, "IAM.CACHE_INVALID", "共享 IAM 缓存无法校验，请检查主密钥与缓存状态") from None

    def _shared(self, scope: str, load: Callable[[], TokenRecord], options: dict, timeout: float) -> TokenRecord:
        settings = get_settings()
        assert settings.redis_url is not None
        if not settings.master_key:
            raise HcsError(0, "IAM.CACHE_KEY", "共享 IAM 缓存需要配置 MASTER_KEY")
        try:
            encrypt("")  # 先校验主密钥，错误配置不得先向 IAM 换发。
        except Exception:
            raise HcsError(0, "IAM.CACHE_KEY", "共享 IAM 缓存主密钥无效") from None
        version = hashlib.sha256((scope + ":" + settings.master_key).encode()).hexdigest()
        prefix = f"inspector:iam:{{{version}}}:"
        data_key, lock_key, owner = prefix + "value", prefix + "lock", uuid.uuid4().hex
        try:
            backend = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=0.25, socket_timeout=0.25)
        except (redis.RedisError, ValueError):
            raise HcsError(0, "IAM.CACHE_UNAVAILABLE", "共享 IAM 缓存配置暂不可用，未向 IAM 认证") from None
        held = False
        deadline = time.monotonic() + remaining_time(timeout + 15)
        try:
            while True:
                cached = self._read(backend, data_key, scope)
                if self._accepted(cached, time.time(), **options):
                    assert cached is not None
                    return cached
                if cached is not None and (cached.generation == options["rejected_generation"] or
                        options["rejected_generation"] is None and cached.token == options["rejected_token"]):
                    backend.eval(_INVALIDATE, 1, data_key, cached.generation)
                if backend.set(lock_key, owner, nx=True, px=180000):
                    held = True
                    break
                if time.monotonic() >= deadline:
                    raise HcsError(0, "IAM.CACHE_BUSY", "IAM 换发正在处理，请稍后重试")
                time.sleep(0.05)
            cached = self._read(backend, data_key, scope)
            if self._accepted(cached, time.time(), **options):
                assert cached is not None
                return cached
            try:
                result = load()
                if options["force"] and not options["refresh"]:
                    result = replace(result, checked_at=time.time())
            except HcsError as error:
                self._publish(backend, data_key, lock_key, owner, scope, None, error)
                raise
            try:
                self._publish(backend, data_key, lock_key, owner, scope, result, None)
            except (redis.RedisError, HcsError):
                # 已取得且尚未过期的 Token 留在本地；Redis 故障不得触发第二次 IAM 请求。
                log.warning("iam.cache.publish_failed", extra={"cloud_service": "iam", "error_code": "IAM.CACHE_WRITE"})
            return result
        except redis.RedisError:
            raise HcsError(0, "IAM.CACHE_UNAVAILABLE", "共享 IAM 缓存暂不可用，未重复向 IAM 认证") from None
        finally:
            if held:
                try:
                    backend.eval(_UNLOCK, 1, lock_key, owner)
                except redis.RedisError:
                    pass
            try:
                backend.close()
            except redis.RedisError:
                pass

    @staticmethod
    def _publish(backend, data_key: str, lock_key: str, owner: str, scope: str,
                 record: TokenRecord | None, error: HcsError | None) -> None:
        if record is not None:
            body = {"kind": "token", "scope": scope, "token": record.token, "expires_at": record.expires_at,
                    "refresh_at": record.refresh_at, "obtained_at": record.obtained_at, "generation": record.generation,
                    "checked_at": record.checked_at}
            generation, expires = record.generation, record.expires_at
        else:
            assert error is not None
            expires, generation = time.time() + TokenCache.FAILURE_SECONDS, uuid.uuid4().hex
            body = {"kind": "failure", "scope": scope, "expires_at": expires, "status": error.status, "code": error.code}
        if expires <= time.time():
            raise HcsError(201, "IAM.INVALID_EXPIRY", "IAM Token 在共享前已过期")
        try:
            cipher = encrypt(json.dumps(body, ensure_ascii=True, separators=(",", ":"), allow_nan=False))
        except Exception:
            raise HcsError(0, "IAM.CACHE_KEY", "共享 IAM 缓存无法加密，请检查主密钥") from None
        if not backend.eval(_PUBLISH, 2, lock_key, data_key, owner, generation, cipher, int(expires * 1000)):
            raise HcsError(0, "IAM.CACHE_LOCK", "IAM 换发租约已失效，未覆盖共享缓存")
