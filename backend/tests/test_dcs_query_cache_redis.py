"""QueryCache 通用 Redis 发布、隔离、容量和并发；仅显式短命测试 Redis。

使用合成 getter，不将缓存测试解释为 DCS 远程历史或 AOM 采样能力。
AOM 旧入口独立验证为停用，不查询缓存、认证或网络。
"""
import multiprocessing
import random
import string
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.collectors.base import RunContext
from app.core.config import get_settings
from app.hcs import HcsError, query_cache
from app.hcs.query_cache import QueryCache
from app.metrics import aom

from .test_cloud_shared_redis import shared_redis as shared_redis


def test_redis_cache_reuses_results_across_cache_instances(shared_redis, monkeypatch):
    url, _service, _backend, _limiter = shared_redis
    monkeypatch.setattr(get_settings(), "redis_url", url)
    key, calls = uuid.uuid4().hex, []
    def load():
        calls.append(1)
        return {"failed": False, "rows": {"synthetic": [[None, 0, 25]]}}
    first, source = QueryCache().get(key, load)
    second, second_source = QueryCache().get(key, load)
    assert first == second and calls == [1] and source == "miss" and second_source == "shared_hit"


def test_redis_publish_enforces_total_bytes_without_evicting_active_lock(shared_redis, monkeypatch):
    url, _service, backend, _limiter = shared_redis
    monkeypatch.setattr(get_settings(), "redis_url", url)
    cache, rng = QueryCache(), random.Random(91)
    cache.MAX_CACHE_BYTES = 2000
    lock_key = "inspector:history:{cache}:lock:" + uuid.uuid4().hex
    backend.set(lock_key, "synthetic-owner", ex=30)
    try:
        for _ in range(8):
            key = uuid.uuid4().hex
            text = "".join(rng.choices(string.ascii_letters, k=2000))
            cache.get(key, lambda text=text: {"failed": False, "value": text})
            assert int(backend.hget("inspector:history:{cache}:sizes", "_bytes")) <= 2000
            assert backend.zcard("inspector:history:{cache}:entries") <= 128
            assert backend.get(lock_key) == b"synthetic-owner"
    finally:
        backend.delete(lock_key)


def _cache_worker(url, key, ready, output):
    import redis

    get_settings().redis_url = url
    backend = redis.Redis.from_url(url)
    try:
        assert ready.wait(10)
        def load():
            backend.incr("test-history-loads:" + key)
            time.sleep(0.2)
            return {"failed": False, "rows": {"synthetic": [[0, None]]}}
        result, source = QueryCache().get(key, load)
        output.put((result, source))
    finally:
        backend.close()


def test_same_query_is_loaded_once_across_real_processes(shared_redis):
    url, _service, backend, _limiter = shared_redis
    context = multiprocessing.get_context("spawn")
    key, ready, output = uuid.uuid4().hex, context.Event(), context.Queue()
    children = [context.Process(target=_cache_worker, args=(url, key, ready, output)) for _ in range(2)]
    try:
        for child in children:
            child.start()
        ready.set()
        for child in children:
            child.join(15)
            assert child.exitcode == 0
        values = [output.get(timeout=2) for _ in range(2)]
        assert values[0][0] == values[1][0]
        assert {value[1] for value in values} == {"miss", "shared_hit"}
        assert int(backend.get("test-history-loads:" + key)) == 1
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(2)
        output.close()
        backend.delete("test-history-loads:" + key)


@pytest.mark.parametrize("failed", [False, True], ids=["complete", "partial"])
def test_generic_results_reuse_real_zero_and_gaps_with_appropriate_shared_ttl(shared_redis, monkeypatch, failed):
    url, _service, backend, _limiter = shared_redis
    monkeypatch.setattr(get_settings(), "redis_url", url)
    key, calls = uuid.uuid4().hex, []
    payload = {"failed": failed, "rows": {"synthetic": [[0, 7], [None, None], [25, 30]]}}
    def load():
        calls.append(1)
        return payload
    cache = QueryCache()
    first, source = cache.get(key, load)
    second, second_source = QueryCache().get(key, load)
    assert first == second == payload and calls == [1]
    assert source == "miss" and second_source == "shared_hit"
    entry = cache.entries[(url, key)]
    ttl = 2 if failed else 30
    assert 0 < entry.expires-time.monotonic() <= ttl
    assert 0 < backend.pttl("inspector:history:{cache}:data:"+key) <= ttl*1000
    # Returned dictionaries are copies: editing one read cannot rewrite Redis or local cache.
    first["rows"]["synthetic"][0][0] = 99
    again, _status = QueryCache().get(key, load)
    local, local_source = cache.get(key, load)
    assert again == local == payload and local_source == "hit" and calls == [1]
    assert local["rows"]["synthetic"] == [[0, 7], [None, None], [25, 30]]


def test_generic_partial_entry_reloads_after_expiry_without_filling_missing_values(shared_redis, monkeypatch):
    url, _service, backend, _limiter = shared_redis
    monkeypatch.setattr(get_settings(), "redis_url", url)
    key, calls, cache = uuid.uuid4().hex, [], QueryCache()
    def load():
        calls.append(1)
        return {"failed": True, "rows": {"synthetic": [[0, None]]}, "attempt": len(calls)}
    first, _source = cache.get(key, load)
    assert first["attempt"] == 1 and first["rows"]["synthetic"] == [[0, None]]
    data_key = "inspector:history:{cache}:data:"+key
    assert 0 < backend.pttl(data_key) <= 2000
    assert backend.pexpire(data_key, 1)
    deadline = time.monotonic()+2
    while backend.exists(data_key) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not backend.exists(data_key)
    cache.entries[(url, key)].expires = 0
    second, source = cache.get(key, load)
    assert second["attempt"] == 2 and source == "miss" and calls == [1, 1]
    assert second["rows"]["synthetic"] == [[0, None]]
    shared, shared_source = QueryCache().get(key, load)
    assert shared == second and shared_source == "shared_hit" and calls == [1, 1]


def test_generic_cache_isolates_distinct_caller_keys_in_shared_and_local_storage(shared_redis, monkeypatch):
    url, _service, _backend, _limiter = shared_redis
    monkeypatch.setattr(get_settings(), "redis_url", url)
    prefix, calls, cache = uuid.uuid4().hex, [], QueryCache()
    scopes = ["tenant-one-project-one", "tenant-two-project-one", "tenant-one-project-two", "changed-config"]
    for index, scope in enumerate(scopes):
        def load(index=index, scope=scope):
            calls.append(scope)
            return {"failed": False, "rows": {"synthetic": [[index, None]]}, "scope": scope}
        value, source = cache.get(prefix+scope, load)
        assert source == "miss" and value["scope"] == scope and value["rows"]["synthetic"] == [[index, None]]
    def forbidden():
        pytest.fail("A published synthetic result must be reused for its exact caller key")
    for index, scope in enumerate(scopes):
        value, source = QueryCache().get(prefix+scope, forbidden)
        local, local_source = cache.get(prefix+scope, forbidden)
        assert source == "shared_hit" and local_source == "hit"
        assert value == local and value["scope"] == scope and value["rows"]["synthetic"] == [[index, None]]
    assert calls == scopes


class ForbiddenAomClient:
    def __getattr__(self, name):
        pytest.fail(f"Disabled AOM inspected a client or network dependency: {name}")


START = datetime(2026, 10, 1, tzinfo=UTC)
MAPPING = {"namespace": "CUSTOM.Legacy", "dimension": "instance_id", "metrics": {"cpu_util": "cpu"}}
DESCRIPTOR = {"namespace": "CUSTOM.Legacy", "metric_name": "cpu", "dimensions": [
    {"name": "instance_id", "value": "synthetic"}]}


@pytest.mark.parametrize("operation", [
    lambda client: aom.aom_probe(client),
    lambda client: aom.aom_list_metrics(client, "CUSTOM.Legacy"),
    lambda client: aom.discover(client, [DESCRIPTOR]),
    lambda client: aom.discover(client, []),
    lambda client: aom.aom_query(client, [DESCRIPTOR], START, START+timedelta(minutes=10)),
    lambda client: aom.aom_query(client, [], START, START+timedelta(minutes=10)),
    lambda client: aom.resolve_series(client, ["synthetic"], aom.AomMapping.model_validate(MAPPING)),
], ids=["probe", "metadata", "discovery", "empty-discovery", "samples", "empty-samples", "resolve"])
def test_disabled_aom_never_reads_query_cache_or_redis(monkeypatch, operation):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Disabled AOM touched the generic query cache or Redis")
    monkeypatch.setattr(get_settings(), "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(QueryCache, "get", forbidden)
    monkeypatch.setattr(QueryCache, "_load", forbidden)
    monkeypatch.setattr(query_cache.redis.Redis, "from_url", forbidden)
    with pytest.raises(HcsError) as error:
        operation(ForbiddenAomClient())
    assert error.value.code == "LOCAL.AOM_DISABLED" and error.value.status == 0
    assert not hasattr(aom, "_cache")


def test_disabled_aom_latest_ignores_old_mapping_without_cache_or_redis(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Disabled AOM latest read must not touch cache or Redis")
    monkeypatch.setattr(get_settings(), "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(QueryCache, "get", forbidden)
    monkeypatch.setattr(query_cache.redis.Redis, "from_url", forbidden)
    tenant = SimpleNamespace(metrics_source="AOM", endpoints={"aom": "https://aom.invalid"},
                             capabilities={"aom": {"available": True}})
    ctx = RunContext(client=ForbiddenAomClient(), account="synthetic", project_id="synthetic", tenant=tenant,
                     params={"aom": MAPPING})
    assert aom.aom_latest(ctx, ["synthetic"]) == {}
    assert ctx.stats["aom_disabled"] is True and ctx.stats["aom_mapping_configured"] is False
    assert ctx.errors == [] and not hasattr(aom, "_cache")
