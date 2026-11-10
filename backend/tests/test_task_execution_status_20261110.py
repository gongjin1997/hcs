"""Queued and cancelled work is neutral; interrupted work preserves failure evidence."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.db import Base
from app.models import AlarmEvent, InspectionRun, InspectionTask, TaskExecution
from app.models.base import utcnow
from app.services import platform_watch, settings_service


@pytest.mark.parametrize("states,expected", [
    (["QUEUED"], "QUEUED"), (["QUEUED", "SUCCESS"], "QUEUED"),
    (["RUNNING", "QUEUED"], "RUNNING"), (["CANCELLED"], "CANCELLED"),
    (["CANCELLED", "SUCCESS"], "PARTIAL"), (["INTERRUPTED"], "FAILED"),
    (["INTERRUPTED", "SUCCESS"], "PARTIAL"), (["FAILED", "QUEUED"], "QUEUED"),
])
def test_run_outcome_never_counts_queued_cancelled_or_interrupted_as_success(states, expected):
    assert platform_watch.run_outcome([SimpleNamespace(status=status) for status in states]) == expected


@pytest.mark.parametrize("neutral", ["QUEUED", "CANCELLED", "RUNNING", "SKIPPED"])
def test_neutral_run_keeps_prior_failure_chain_and_counts(mem_db, neutral):
    now = utcnow()
    for offset, status in ((4, "SUCCESS"), (3, "FAILED"), (2, "INTERRUPTED"), (1, neutral)):
        mem_db.add(InspectionRun(task_code="dcs_status", tenant_id=1, project_ref=11, status=status,
                                started_at=now - timedelta(minutes=offset),
                                finished_at=now - timedelta(minutes=offset) if status not in ("RUNNING", "QUEUED") else None,
                                resource_total=7 if status == "INTERRUPTED" else 0))
    mem_db.commit()
    scopes = platform_watch.task_scopes(mem_db, tenant_ids=[1])
    assert len(scopes) == 1 and scopes[0]["fails"] == 2
    assert scopes[0]["last_status"] == neutral and scopes[0]["effective_status"] == "INTERRUPTED"
    assert scopes[0]["effective_resource_total"] == 7
    assert platform_watch.scope_execution_status(scopes[0]) == "INTERRUPTED"
    assert platform_watch.task_last_status(scopes) == "INTERRUPTED"
    assert platform_watch.task_health(scopes, 2) == "FAILED"


def test_interrupted_raises_failure_alarm_and_neutral_runs_cannot_resolve_it(mem_db):
    now = utcnow()
    mem_db.add(InspectionTask(code="dcs_status", name="缓存巡检", service="dcs", interval_sec=120))
    settings_service.save(mem_db, "platform_alarm", {"enabled": True, "task_fail_threshold": 1}, "test")
    failed = InspectionRun(task_code="dcs_status", tenant_id=1, project_ref=11, status="INTERRUPTED",
                           started_at=now - timedelta(minutes=3), finished_at=now - timedelta(minutes=2),
                           error_summary="worker interrupted")
    mem_db.add(failed)
    mem_db.commit()
    platform_watch.on_run(mem_db, failed)
    mem_db.commit()
    alarm = mem_db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-PLAT-001"))
    assert alarm is not None and alarm.status == "FIRING"
    for index, status in enumerate(("QUEUED", "CANCELLED")):
        row = InspectionRun(task_code="dcs_status", tenant_id=1, project_ref=11, status=status,
                            started_at=now + timedelta(seconds=index),
                            finished_at=now + timedelta(seconds=index) if status == "CANCELLED" else None)
        mem_db.add(row)
        mem_db.commit()
        platform_watch.on_run(mem_db, row)
        mem_db.commit()
        assert alarm.status == "FIRING"


def test_worker_runbook_uses_installed_compose_command():
    runbook = platform_watch.RULES["R-PLAT-004"]["runbook"]
    assert "docker-compose up -d scheduler notifier" in runbook and "docker compose" not in runbook


def watch_run(db, *, status="FAILED", durable=False):
    now = utcnow()
    row = InspectionRun(task_code="dcs_status", tenant_id=1, project_ref=11, status=status,
                        started_at=now - timedelta(seconds=5), finished_at=now,
                        error_summary="synthetic local outcome")
    db.add(row)
    db.flush()
    if durable:
        key = uuid4().hex
        db.add(TaskExecution(id=str(uuid4()), run_id=row.id, task_code=row.task_code, tenant_id=row.tenant_id,
                             project_ref=row.project_ref, status=status, created_at=now, finished_at=now,
                             request_id=key, request_key=key, scope_key=key,
                             queue_expires_at=now + timedelta(minutes=5)))
        db.flush()
    return row


def test_durable_outcome_publishes_once_after_commit_in_a_fresh_session(mem_db, monkeypatch):
    row = watch_run(mem_db, durable=True)
    observed = []
    monkeypatch.setattr(platform_watch, "_on_run", lambda db, run: observed.append((db, run.id, run.status)))
    platform_watch.defer_on_run(mem_db, row.id)
    platform_watch.defer_on_run(mem_db, row.id)
    assert observed == []
    mem_db.commit()
    assert len(observed) == 1 and observed[0][0] is not mem_db
    assert observed[0][1:] == (row.id, "FAILED")
    platform_watch.on_run(mem_db, row)  # Old pipeline/ledger callback is suppressed for durable work.
    mem_db.commit()
    assert len(observed) == 1


def test_deferred_watch_disappears_on_outer_rollback(mem_db, monkeypatch):
    row = watch_run(mem_db)
    observed = []
    monkeypatch.setattr(platform_watch, "_on_run", lambda *_: observed.append(True))
    platform_watch.defer_on_run(mem_db, row.id)
    mem_db.rollback()
    mem_db.commit()
    assert observed == []


def test_nested_commit_waits_until_outer_commit_for_platform_watch(mem_db, monkeypatch):
    row = watch_run(mem_db)
    observed = []
    monkeypatch.setattr(platform_watch, "_on_run", lambda db, run: observed.append(run.id))
    with mem_db.begin_nested():
        platform_watch.defer_on_run(mem_db, row.id)
    assert observed == []
    mem_db.commit()
    assert observed == [row.id]


def test_savepoint_rollback_discards_only_its_own_deferred_watches(mem_db, monkeypatch):
    parent = watch_run(mem_db)
    child = watch_run(mem_db)
    observed = []
    monkeypatch.setattr(platform_watch, "_on_run", lambda db, run: observed.append(run.id))
    platform_watch.defer_on_run(mem_db, parent.id)
    nested = mem_db.begin_nested()
    platform_watch.defer_on_run(mem_db, child.id)
    nested.rollback()
    mem_db.commit()
    assert observed == [parent.id]


def test_watch_failure_cannot_rollback_the_committed_execution(mem_db, monkeypatch):
    row = watch_run(mem_db, durable=True)
    row_id = row.id

    def fail_watch(*_):
        raise RuntimeError("synthetic platform alert failure")

    monkeypatch.setattr(platform_watch, "_on_run", fail_watch)
    platform_watch.defer_on_run(mem_db, row.id)
    mem_db.commit()
    mem_db.expire_all()
    assert mem_db.get(InspectionRun, row_id).status == "FAILED"
    assert mem_db.scalar(select(TaskExecution).where(TaskExecution.run_id == row_id)).status == "FAILED"
    # The callback is drained on the committed transaction, so a later commit does not retry it.
    monkeypatch.setattr(platform_watch, "_on_run", lambda *_: pytest.fail("duplicate watch dispatch"))
    mem_db.commit()


def test_unmanaged_pipeline_keeps_its_existing_on_run_callback(mem_db, monkeypatch):
    row = watch_run(mem_db)
    mem_db.commit()
    observed = []
    monkeypatch.setattr(platform_watch, "_on_run", lambda db, run: observed.append(run.id))
    platform_watch.on_run(mem_db, row)
    assert observed == [row.id]


@pytest.mark.parametrize("status", ["SUCCESS", "PARTIAL", "FAILED", "TIMEOUT", "INTERRUPTED", "CANCELLED", "SKIPPED"])
def test_every_durable_terminal_outcome_can_be_dispatched_after_commit(mem_db, monkeypatch, status):
    row = watch_run(mem_db, status=status, durable=True)
    observed = []
    monkeypatch.setattr(platform_watch, "_on_run", lambda db, run: observed.append(run.status))
    platform_watch.defer_on_run(mem_db, row.id)
    mem_db.commit()
    assert observed == [status]


def test_real_durable_failure_alert_is_emitted_after_terminal_commit_only_once(mem_db):
    from app.services import task_execution

    mem_db.add(InspectionTask(code="dcs_status", name="缓存巡检", service="dcs", interval_sec=120))
    settings_service.save(mem_db, "platform_alarm", {"enabled": True, "task_fail_threshold": 1}, "test")
    run = watch_run(mem_db, status="RUNNING", durable=True)
    run.finished_at = None
    row = mem_db.scalar(select(TaskExecution).where(TaskExecution.run_id == run.id))
    row.finished_at = None
    mem_db.commit()
    task_execution.finish(mem_db, row, "INTERRUPTED", "synthetic worker interrupted")
    platform_watch.defer_on_run(mem_db, run.id)
    assert mem_db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-PLAT-001")) is None
    mem_db.commit()
    alarm = mem_db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-PLAT-001"))
    assert alarm is not None and alarm.status == "FIRING" and alarm.detail["last_failure_run_id"] == run.id
    original_count = alarm.count
    # The old successful return path still invokes on_run after durable finish; it must be a no-op.
    platform_watch.on_run(mem_db, run)
    task_execution.finish(mem_db, row, "FAILED", "late duplicate result")
    mem_db.commit()
    mem_db.refresh(alarm)
    assert alarm.count == original_count and run.status == "INTERRUPTED"


def test_deferred_watch_does_not_reenter_a_busy_single_connection_pool(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'watch.db'}", pool_size=1, max_overflow=0, pool_timeout=.1)
    Base.metadata.create_all(engine)
    observed = []
    monkeypatch.setattr(platform_watch, "_on_run", lambda db, run: observed.append(run.id))
    try:
        with Session(engine, expire_on_commit=False) as db:
            row = watch_run(db, durable=True)
            platform_watch.defer_on_run(db, row.id)
            db.commit()
            assert observed == [row.id]
    finally:
        engine.dispose()
