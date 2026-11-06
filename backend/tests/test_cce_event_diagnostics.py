"""事件计数诊断不进入平台告警；真实故障与未知恢复证据保持原语义。"""
import logging
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.hcs import HcsError
from app.k8s import collector
from app.models import AlarmEvent, InspectionRun, Notification, NotifyRoute
from app.services import pipeline, platform_watch
from app.services.tenancy import resolve_targets

from .test_cce_container_review_20261102 import CL, NOW, Client, event, pod, rows, run
from .test_v04_platform import _setup_route


class TaskClient(Client):
    def __init__(self, pods=(), events=(), *, fail_events=False):
        super().__init__(pods, events)
        self.calls = 0
        self.fail_events = fail_events

    def configure_qps(self, value):
        pass

    def metrics_snapshot(self):
        return {}

    def get(self, service, path, **kwargs):
        self.calls += 1
        if service == "cce":
            assert path == "/api/v3/projects/{project_id}/clusters"
            return {"items": [{"metadata": {"uid": CL[0], "name": CL[1]}, "status": {"phase": "Available"}}]}
        if self.fail_events and path == "/api/v1/events":
            raise HcsError(503, "TEST.UNAVAILABLE", "synthetic unavailable")
        return super().get(service, path, **kwargs)


@pytest.fixture
def collect_task(world, mem_db, monkeypatch):
    target = next(t for t in resolve_targets(mem_db) if t.code == "tenant-a")
    _setup_route(mem_db)
    mem_db.add(NotifyRoute(name="CCE 合成测试", priority=2, match={"services": ["cce"]}, group_code="leaders"))
    mem_db.commit()

    def collect(*, events=(), pods=None, minute=0, fail_events=False):
        at = NOW + timedelta(minutes=minute)
        for module in (collector, pipeline, platform_watch):
            monkeypatch.setattr(module, "utcnow", lambda: at)
        return pipeline.run_task(mem_db, collector.TASK_CODE, target=target,
                                 client=TaskClient([pod()] if pods is None else pods, events, fail_events=fail_events))

    return collect, target


@pytest.mark.parametrize("issue", ["invalid_count", "invalid_range", "future_time", "missing_time", "crossing_window"])
def test_event_data_diagnostics_do_not_fail_task_or_create_notifications(mem_db, collect_task, issue, caplog):
    collect, _ = collect_task
    caplog.set_level(logging.INFO, logger="app.k8s.collector")
    item = event()
    if issue == "invalid_count":
        item["count"] = 0
    elif issue == "invalid_range":
        item["firstTimestamp"] = (NOW + timedelta(minutes=1)).isoformat()
    elif issue == "future_time":
        item["lastTimestamp"] = (NOW + timedelta(hours=1)).isoformat()
    elif issue == "missing_time":
        item.pop("firstTimestamp")
        item.pop("lastTimestamp")
    else:
        item = event(count=100, first=NOW - timedelta(days=2))
    for minute in range(4):
        result = collect(events=[item], minute=minute)
        assert result.status == "SUCCESS"
        assert result.error_summary is None
        assert result.stats.get("event_data_diagnostics") or result.stats.get("event_window_unknown_checks")
    assert list(mem_db.scalars(select(AlarmEvent))) == []
    assert list(mem_db.scalars(select(Notification))) == []
    diagnostics = [r for r in caplog.records if getattr(r, "event", "") == "collection_data_unavailable"]
    assert diagnostics and all(r.levelno == logging.INFO for r in diagnostics)


@pytest.mark.parametrize("legacy_count", [0, None])
def test_modern_singleton_uses_event_time_and_one_occurrence(mem_db, legacy_count):
    item = event(count=legacy_count)
    item.update(firstTimestamp=None, lastTimestamp=None, eventTime=NOW.isoformat(), series=None)
    current, observations = run(mem_db, [pod()], [item])
    [stored] = rows(mem_db, "EVICTED")
    assert (stored.count, stored.first_time, stored.last_time) == (1, NOW, NOW)
    assert not current.ctx.errors
    assert next(o.facts["evicted_1h"] for o in observations if o.check_item == "pod_evicted") == 1


def test_modern_singleton_with_omitted_count_uses_event_time_before_server_creation(mem_db):
    item = event()
    item.pop("count")
    item.update(firstTimestamp=None, lastTimestamp=None, eventTime=NOW.isoformat())
    item["metadata"]["creationTimestamp"] = (NOW + timedelta(seconds=2)).isoformat()
    current, _ = run(mem_db, [pod()], [item])
    [stored] = rows(mem_db, "EVICTED")
    assert (stored.count, stored.first_time, stored.last_time) == (1, NOW, NOW)
    assert not current.ctx.errors and not current.event_diagnostics


def test_series_fields_take_precedence_over_unused_legacy_fields(mem_db):
    first = NOW - timedelta(minutes=5)
    item = event(count=0, first=NOW, last=NOW - timedelta(days=1))
    item.update(eventTime=first.isoformat(), series={"count": 4, "lastObservedTime": NOW.isoformat()})
    current, _ = run(mem_db, [pod()], [item])
    [stored] = rows(mem_db, "EVICTED")
    assert (stored.count, stored.first_time, stored.last_time) == (4, first, NOW)
    assert not current.ctx.errors and not current.event_diagnostics


@pytest.mark.parametrize("count", [True, False, -1, "0", "2", 1.5])
def test_modern_singleton_does_not_coerce_invalid_counts(mem_db, count):
    item = event(count=count)
    item.update(eventTime=NOW.isoformat(), series=None)
    current, observations = run(mem_db, [pod()], [item])
    assert not rows(mem_db) and not current.ctx.errors
    assert current.ctx.stats["event_data_diagnostics"] == {"event_count_invalid": 1}
    assert not any(o.check_item == "pod_evicted" for o in observations)


@pytest.mark.parametrize("series,reason", [
    ({}, "event_time_missing"),
    ({"count": 4}, "event_time_missing"),
    ({"count": 0, "lastObservedTime": NOW.isoformat()}, "event_count_invalid"),
    ({"count": None, "lastObservedTime": NOW.isoformat()}, "event_count_invalid"),
])
def test_incomplete_series_is_diagnostic_without_legacy_fallback(mem_db, series, reason):
    item = event(count=9)
    item.update(eventTime=NOW.isoformat(), series=series)
    current, _ = run(mem_db, [pod()], [item])
    assert not rows(mem_db) and not current.ctx.errors
    assert current.ctx.stats["event_data_diagnostics"] == {reason: 1}


def test_invalid_event_diagnostics_are_aggregated_without_payload(mem_db, caplog):
    caplog.set_level(logging.INFO, logger="app.k8s.collector")
    events = [event(uid=f"invalid-{index}", count=0) for index in range(8)]
    for item in events:
        item["message"] = "sensitive-test-message-should-not-be-logged"
    current, _ = run(mem_db, [pod()], events)
    diagnostics = [r for r in caplog.records if getattr(r, "phase", "") == "event_validation"]
    assert len(diagnostics) == 1 and diagnostics[0].levelno == logging.INFO
    assert diagnostics[0].reason == "event_count_invalid" and diagnostics[0].affected_count == 8
    assert "sensitive-test-message" not in caplog.text and "invalid-0" not in caplog.text
    assert not current.ctx.errors


@pytest.mark.parametrize("changed,reason", [
    ({"count": 2}, "event_counter_regressed"),
    ({"lastTimestamp": (NOW - timedelta(seconds=1)).isoformat()}, "event_time_regressed"),
])
def test_stale_same_uid_preserves_evidence_without_task_failure(mem_db, changed, reason):
    item = event(count=5, first=NOW - timedelta(minutes=1))
    run(mem_db, [pod()], [item])
    current, observations = run(mem_db, [pod()], [{**item, **changed}], now=NOW + timedelta(minutes=1))
    [stored] = rows(mem_db, "EVICTED")
    assert (stored.count, stored.last_time) == (5, NOW)
    assert not current.ctx.errors and current.ctx.stats["event_data_diagnostics"] == {reason: 1}
    assert not any(o.check_item == "pod_evicted" for o in observations)


def test_real_crashloop_still_fires_when_event_count_is_invalid(mem_db, collect_task):
    collect, _ = collect_task
    result = collect(pods=[pod(ready=False, state={"waiting": {"reason": "CrashLoopBackOff"}})],
                     events=[event(count=0)])
    assert result.status == "SUCCESS"
    [alarm] = list(mem_db.scalars(select(AlarmEvent)))
    assert alarm.rule_id == "R-CCE-POD-003" and alarm.status == "FIRING"
    assert mem_db.scalar(select(Notification).where(Notification.alarm_id == alarm.id)) is not None


@pytest.mark.parametrize("uncertain", ["invalid_count", "crossing_window"])
def test_uncertain_event_counts_cannot_resolve_a_real_alarm(mem_db, collect_task, uncertain):
    collect, _ = collect_task
    collect(events=[event()])
    alarm = mem_db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-CCE-POD-005"))
    assert alarm is not None and alarm.status == "FIRING"
    item = (event(count=0) if uncertain == "invalid_count" else
            event(uid="older-event", count=100, first=NOW - timedelta(days=2)))
    result = collect(events=[item], minute=1)
    assert result.status == "SUCCESS"
    mem_db.refresh(alarm)
    assert alarm.status == "FIRING" and alarm.resolved_at is None
    assert mem_db.scalar(select(Notification).where(
        Notification.alarm_id == alarm.id, Notification.kind == "RESOLVED")) is None


def test_event_api_failure_still_raises_platform_alarm(mem_db, collect_task):
    collect, _ = collect_task
    for minute in range(3):
        result = collect(fail_events=True, minute=minute)
        assert result.status == "PARTIAL" and "503" in result.error_summary
    alarm = mem_db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-PLAT-001"))
    assert alarm is not None and alarm.status == "FIRING"
    assert mem_db.scalar(select(Notification).where(Notification.alarm_id == alarm.id)) is not None


def test_successful_collection_resolves_old_diagnostic_task_alarm(mem_db, collect_task):
    collect, target = collect_task
    for minute in (-4, -3, -2):
        at = NOW + timedelta(minutes=minute)
        result = InspectionRun(task_code=collector.TASK_CODE, tenant_id=target.tenant_id,
                               project_ref=target.project_ref, started_at=at, finished_at=at, status="PARTIAL",
                               error_summary="容器事件累计次数跨越统计窗口，未输出推测次数")
        mem_db.add(result)
        mem_db.commit()
        platform_watch.on_run(mem_db, result)
    alarm = mem_db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-PLAT-001"))
    assert alarm is not None and alarm.status == "FIRING"
    result = collect(events=[event(count=100, first=NOW - timedelta(days=2))])
    assert result.status == "SUCCESS"
    mem_db.refresh(alarm)
    assert alarm.status == "RESOLVED"
