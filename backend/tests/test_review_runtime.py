"""跨模块运行边界：显式租户范围不可退回全局账号。"""
import pytest

from app.services.tenancy import resolve_targets


@pytest.mark.parametrize("tenant_ids", [[], [1], [99999]])
def test_explicit_scope_never_uses_legacy_account(mem_db, tenant_ids):
    assert resolve_targets(mem_db, tenant_ids) == []


def test_only_unrestricted_empty_installation_uses_legacy(mem_db):
    targets = resolve_targets(mem_db)
    assert len(targets) == 1
    assert targets[0].tenant_id is None


def test_empty_scope_stays_empty_with_configured_tenants(world, mem_db):
    assert resolve_targets(mem_db, []) == []
    assert len(resolve_targets(mem_db)) == 2


def test_long_job_renews_lease_and_closes_client(monkeypatch):
    from threading import Event
    from unittest.mock import Mock

    from app.workers import scheduler

    renewed = Event()
    lock = Mock()
    lock.extend.side_effect = lambda *_args, **_kwargs: renewed.set()
    monkeypatch.setattr(scheduler, "_locked", lambda *_args: lock)
    scheduler._with_lock("slow-test", 1, lambda: renewed.wait(2) or pytest.fail("任务执行期间未续租"))
    lock.extend.assert_called_with(1, replace_ttl=True)
    lock.release.assert_called_once()
    lock.redis.close.assert_called_once()


def test_release_error_does_not_mask_job_failure(monkeypatch):
    from unittest.mock import Mock

    from app.workers import scheduler

    lock = Mock()
    lock.release.side_effect = RuntimeError("lock unavailable")
    monkeypatch.setattr(scheduler, "_locked", lambda *_args: lock)

    def fail():
        raise ValueError("task error")

    with pytest.raises(ValueError, match="task error"):
        scheduler._with_lock("failure", 60, fail)
    lock.redis.close.assert_called_once()


def test_unacquired_lock_closes_redis_and_skips_job(monkeypatch):
    from unittest.mock import Mock

    from app.core.config import get_settings
    from app.workers import scheduler

    client = Mock()
    client.lock.return_value.acquire.return_value = False
    factory = Mock(return_value=client)
    monkeypatch.setattr(get_settings(), "redis_url", "redis://test.invalid:6379")
    monkeypatch.setattr("redis.Redis.from_url", factory)
    job = Mock()
    scheduler._with_lock("occupied", 60, job)
    job.assert_not_called()
    client.close.assert_called_once()
    assert client.lock.call_args.kwargs["thread_local"] is False


def test_pubsub_closes_connections_on_cancel(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, Mock

    from app.core import events
    from app.core.config import get_settings

    client, pubsub = AsyncMock(), AsyncMock()
    client.__aenter__.return_value = client
    pubsub.__aenter__.return_value = pubsub
    client.pubsub = Mock(return_value=pubsub)

    async def listen():
        raise asyncio.CancelledError
        yield  # pragma: no cover - async iterator shape

    pubsub.listen = listen
    monkeypatch.setattr(get_settings(), "redis_url", "redis://test.invalid:6379")
    monkeypatch.setattr("redis.asyncio.Redis.from_url", lambda *_args: client)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(events.redis_forwarder())
    pubsub.__aexit__.assert_awaited_once()
    client.__aexit__.assert_awaited_once()


def test_partial_run_is_warning_and_not_metrics_success(mem_db, monkeypatch):
    from contextlib import nullcontext
    from datetime import timedelta

    from app.api import metrics
    from app.models import InspectionRun, InspectionTask
    from app.models.base import utcnow
    from app.tools import status

    now = utcnow()
    successful = now - timedelta(hours=2)
    mem_db.add(InspectionTask(code="runtime-review", name="runtime-review", service="dcs", interval_sec=120))
    for at, result in ((successful, "SUCCESS"), (now - timedelta(hours=1), "PARTIAL")):
        mem_db.add(InspectionRun(task_code="runtime-review", started_at=at, finished_at=at, status=result))
    mem_db.commit()
    assert status.collect(mem_db)["tasks"][0]["status"] == "WARN"
    monkeypatch.setattr(metrics, "SessionLocal", lambda: nullcontext(mem_db))
    lines = metrics.collect().splitlines()
    actual = next(line for line in lines if line.startswith("hcs_inspector_task_last_success_timestamp_seconds{"))
    assert actual.endswith(f" {int(successful.timestamp())}")
