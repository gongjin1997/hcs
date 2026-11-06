"""SSE 发送前重新验证会话、权限和最新租户范围，不访问实际认证库。"""
import asyncio

import pytest

from app.api.v1 import stream
from app.core.deps import CurrentUser
from app.core.events import Broadcaster
from app.core.response import BizError, ErrorCode


class Request:
    def __init__(self, iterations):
        self.iterations = iterations

    async def is_disconnected(self):
        self.iterations -= 1
        return self.iterations < 0


def test_stream_uses_refreshed_tenant_scope_before_delivering_events(monkeypatch):
    bus = Broadcaster()
    user = CurrentUser(id=1, username="reviewer", display_name="reviewer", tenant_ids=[1], permissions=["alarm:view"])
    monkeypatch.setattr(stream, "broadcaster", bus)
    monkeypatch.setattr(stream, "user_from_ticket", lambda ticket: user)
    monkeypatch.setattr(stream, "refresh_session", lambda old: old.model_copy(update={"tenant_ids": [2]}))

    async def run():
        response = await stream.alarm_stream(Request(2), "test-ticket", None)
        bus._deliver({"alarm": {"tenant_id": 1, "id": 11}})
        bus._deliver({"alarm": {"tenant_id": 2, "id": 22}})
        return [frame async for frame in response.body_iterator]

    frames = asyncio.run(run())
    assert len(frames) == 2 and '"id": 22' in frames[1] and '"id": 11' not in "".join(frames)
    assert bus.count == 0


@pytest.mark.parametrize("revoked", [True, False])
def test_stream_closes_when_session_or_view_permission_is_revoked(monkeypatch, revoked):
    bus = Broadcaster()
    user = CurrentUser(id=1, username="reviewer", display_name="reviewer", permissions=["alarm:view"])
    monkeypatch.setattr(stream, "broadcaster", bus)
    monkeypatch.setattr(stream, "user_from_ticket", lambda ticket: user)

    def refresh(old):
        if revoked:
            raise BizError(ErrorCode.UNAUTHORIZED, "session revoked", 401)
        return old.model_copy(update={"permissions": []})

    monkeypatch.setattr(stream, "refresh_session", refresh)

    async def run():
        response = await stream.alarm_stream(Request(1), "test-ticket", None)
        bus._deliver({"alarm": {"tenant_id": 1}})
        return [frame async for frame in response.body_iterator]

    assert asyncio.run(run()) == ["retry: 5000\n\n"]
    assert bus.count == 0
