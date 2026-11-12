"""T08：告警事件的发布端与订阅端复用 Redis 客户端；API 默认单进程，/metrics 计数不跳变。"""
import asyncio
import json
import re
import uuid
from contextlib import suppress
from pathlib import Path
from threading import Barrier, Thread
from unittest.mock import AsyncMock, Mock

import pytest
import redis.asyncio

from app.core import events
from app.core.config import get_settings

from .test_cloud_shared_redis import shared_redis as shared_redis

ROOT = Path(__file__).resolve().parents[2]
URL = "redis://test.invalid:6379/0"
OTHER_URL = "redis://other.invalid:6379/1"
HTTP_SAMPLE = re.compile(r"^(hcs_inspector_http_request\S*) (\S+)$", re.M)


class FakeClient:
    """替代 redis.Redis：只记录调用，不建立连接。"""

    def __init__(self, url: str, options: dict):
        self.url, self.options = url, options
        self.published: list[tuple[str, str]] = []
        self.closed = 0
        self.error: Exception | None = None

    def publish(self, channel: str, payload: str) -> int:
        if self.error is not None:
            raise self.error
        self.published.append((channel, payload))
        return 1

    def close(self) -> None:
        self.closed += 1


@pytest.fixture()
def created(monkeypatch):
    """发布端创建过的全部客户端；每个用例从空的模块级缓存开始，结束后还原。"""
    clients: list[FakeClient] = []

    def from_url(url, **options):
        client = FakeClient(url, options)
        clients.append(client)
        return client

    monkeypatch.setattr(get_settings(), "redis_url", URL)
    monkeypatch.setattr("redis.Redis.from_url", from_url)
    monkeypatch.setattr(events, "_publisher", None)
    monkeypatch.setattr(events, "_publish_failures", 0)
    monkeypatch.setattr(events, "_publish_warned_at", 0.0)
    monkeypatch.setattr(events.broadcaster, "publish_threadsafe", Mock())
    return clients


def ids(client: FakeClient) -> list[int]:
    assert all(channel == events.CHANNEL for channel, _ in client.published)
    return [json.loads(payload)["alarm"]["id"] for _, payload in client.published]


def test_publishing_1000_events_creates_one_client(created):
    # 任务书 T08 验收：publish 调用 1000 次只创建 1 个客户端。
    for index in range(1000):
        events.publish({"alarm": {"id": index}})

    assert len(created) == 1
    assert ids(created[0]) == list(range(1000)) and created[0].closed == 0
    assert created[0].url == URL and created[0].options == {"socket_connect_timeout": 5, "socket_timeout": 5}
    events.broadcaster.publish_threadsafe.assert_not_called()


def test_concurrent_publishers_share_one_client(created):
    workers, per_worker = 8, 50
    barrier = Barrier(workers)

    def publish():
        barrier.wait(5)
        for index in range(per_worker):
            events.publish({"alarm": {"id": index}})

    threads = [Thread(target=publish) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(created) == 1 and len(created[0].published) == workers * per_worker


def test_failed_publish_closes_the_client_and_the_next_publish_rebuilds_it(created):
    events.publish({"alarm": {"id": 1}})
    created[0].error = ConnectionError("connection reset")

    events.publish({"alarm": {"id": 2}})  # 失败：退化为进程内广播，并丢弃这个客户端。

    assert created[0].closed == 1 and events._publisher is None
    events.broadcaster.publish_threadsafe.assert_called_once_with({"alarm": {"id": 2}})

    events.publish({"alarm": {"id": 3}})
    events.publish({"alarm": {"id": 4}})

    assert len(created) == 2 and events._publisher == (URL, created[1])
    assert ids(created[1]) == [3, 4] and created[1].closed == 0 and created[0].closed == 1


def test_changed_redis_url_replaces_the_client_and_closes_the_old_one(created, monkeypatch):
    events.publish({"alarm": {"id": 1}})
    monkeypatch.setattr(get_settings(), "redis_url", OTHER_URL)

    events.publish({"alarm": {"id": 2}})
    events.publish({"alarm": {"id": 3}})

    assert [client.url for client in created] == [URL, OTHER_URL]
    assert (created[0].closed, created[1].closed) == (1, 0) and ids(created[1]) == [2, 3]


def test_unserializable_event_does_not_discard_the_healthy_client(created):
    events.publish({"alarm": {"id": 1}})
    circular: dict = {}
    circular["self"] = circular

    events.publish(circular)

    assert len(created) == 1 and created[0].closed == 0 and events._publisher == (URL, created[0])
    assert events.broadcaster.publish_threadsafe.call_args.args[0] is circular


def test_without_redis_events_stay_in_process_and_no_client_is_created(created, monkeypatch):
    monkeypatch.setattr(get_settings(), "redis_url", None)

    events.publish({"alarm": {"id": 1}})

    assert created == []
    events.broadcaster.publish_threadsafe.assert_called_once_with({"alarm": {"id": 1}})


def subscriber(monkeypatch, outcomes):
    """订阅端的假客户端：第 n 次 pubsub() 的监听按 outcomes[n] 结束（异常，或先收到的消息列表）。"""
    client = AsyncMock()
    client.__aenter__.return_value = client
    subscriptions: list[AsyncMock] = []

    def pubsub():
        outcome = outcomes[len(subscriptions)]
        subscription = AsyncMock()
        subscription.__aenter__.return_value = subscription

        async def listen():
            if isinstance(outcome, BaseException):
                raise outcome
            for message in outcome:
                yield message

        subscription.listen = listen
        subscriptions.append(subscription)
        return subscription

    client.pubsub = Mock(side_effect=pubsub)
    factory = Mock(return_value=client)
    monkeypatch.setattr(get_settings(), "redis_url", URL)
    monkeypatch.setattr("redis.asyncio.Redis.from_url", factory)
    monkeypatch.setattr(events, "FORWARDER_RETRY_SEC", 0)
    return factory, client, subscriptions


def test_forwarder_keeps_one_client_across_reconnects(monkeypatch):
    delivered: list[dict] = []
    monkeypatch.setattr(events.broadcaster, "_deliver", delivered.append)
    messages = [{"type": "subscribe", "data": 1}, {"type": "message", "data": b'{"alarm": {"id": 7}}'}]
    factory, client, subscriptions = subscriber(
        monkeypatch, [ConnectionError("connection lost"), messages, asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(events.redis_forwarder())

    factory.assert_called_once_with(URL)
    assert len(subscriptions) == 3 and delivered == [{"alarm": {"id": 7}}]
    for subscription in subscriptions:
        subscription.subscribe.assert_awaited_once_with(events.CHANNEL)
        subscription.__aexit__.assert_awaited_once()
    client.__aenter__.assert_awaited_once()
    client.__aexit__.assert_awaited_once()


def test_forwarder_retries_client_setup_until_it_succeeds(monkeypatch):
    factory, client, subscriptions = subscriber(monkeypatch, [asyncio.CancelledError()])
    client.__aenter__.side_effect = [ConnectionError("redis unavailable"), client]

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(events.redis_forwarder())

    assert factory.call_count == 2 and len(subscriptions) == 1
    client.__aexit__.assert_awaited_once()


def test_real_redis_reconnects_without_creating_new_clients(shared_redis, monkeypatch):
    # 可选的真实 Redis 验证：服务端断开两端的连接后事件继续送达，发布端与订阅端都不新建客户端。
    url, _service, backend, _limiter = shared_redis
    made = {"publisher": 0, "subscriber": 0}
    delivered: list[dict] = []

    def counted(kind, factory):
        def from_url(*args, **kwargs):
            made[kind] += 1
            return factory(*args, **kwargs)
        return from_url

    monkeypatch.setattr(get_settings(), "redis_url", url)
    monkeypatch.setattr(events, "CHANNEL", f"inspector:test:{uuid.uuid4().hex}")
    monkeypatch.setattr(events, "FORWARDER_RETRY_SEC", 0.05)
    monkeypatch.setattr(events, "_publisher", None)
    monkeypatch.setattr(events.broadcaster, "_deliver", delivered.append)
    monkeypatch.setattr("redis.Redis.from_url", counted("publisher", redis.Redis.from_url))
    monkeypatch.setattr("redis.asyncio.Redis.from_url", counted("subscriber", redis.asyncio.Redis.from_url))

    def subscriptions() -> set[int]:
        return {int(row["id"]) for row in backend.client_list(_type="pubsub")}

    others = subscriptions()  # 测试 Redis 上已有的其他订阅连接不受影响：只断开本用例建立的连接。

    async def deliver(event_id: int) -> None:
        event = {"alarm": {"id": event_id}}
        for _ in range(100):  # 订阅建立之前发布的事件不会送达：重复发布直到收到。
            await asyncio.to_thread(events.publish, event)
            await asyncio.sleep(0.05)
            if event in delivered:
                return
        pytest.fail("事件没有经 Redis 送达")

    async def scenario() -> tuple[set[int], set[int], int, int]:
        forwarder = asyncio.create_task(events.redis_forwarder())
        try:
            await deliver(1)
            publisher = events._publisher[1]
            subscribed, connection = subscriptions() - others, publisher.client_id()
            for client_id in (*subscribed, connection):
                backend.client_kill_filter(_id=client_id)
            await deliver(2)
            return subscribed, subscriptions() - others, connection, publisher.client_id()
        finally:
            forwarder.cancel()
            with suppress(asyncio.CancelledError):
                await forwarder

    try:
        before, after, connection, reconnected = asyncio.run(scenario())
    finally:
        if events._publisher is not None:
            events._publisher[1].close()
    assert len(before) == len(after) == 1 and before != after, "订阅连接被断开后已重建"
    assert reconnected != connection, "发布连接被断开后已重建"
    assert made == {"publisher": 1, "subscriber": 1}


def http_series(client) -> dict[str, float]:
    response = client.get("/metrics")
    assert response.status_code == 200
    return {name: float(value) for name, value in HTTP_SAMPLE.findall(response.text)}


def test_consecutive_scrapes_never_show_decreasing_request_counters(client):
    # 任务书 T08 验收：连续两次抓取 /metrics 计数单调不减（同一进程内；部署默认单进程见下一个用例）。
    first = http_series(client)
    client.get("/api/v1/auth/me")
    second = http_series(client)
    third = http_series(client)

    count = "hcs_inspector_http_request_duration_seconds_count"
    assert count in first and set(first) <= set(second) == set(third)
    assert all(second[name] >= value for name, value in first.items())
    assert all(third[name] >= value for name, value in second.items())
    assert second[count] == first[count] + 1 and third[count] == second[count], "抓取 /metrics 本身不计数"


def test_api_runs_a_single_worker_unless_configured_otherwise():
    # 请求计数是进程内变量：多个 worker 共用一个端口时，抓取结果会在各进程的计数之间跳变（任务书 C06）。
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    assert "--workers ${API_WORKERS:-1}" in dockerfile
    template = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
    assert re.findall(r"^API_WORKERS=(.*)$", template, re.M) == ["1"]
