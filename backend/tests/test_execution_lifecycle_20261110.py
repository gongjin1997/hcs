"""队列状态、提交栅栏与旧记录恢复使用真实数据库事务。"""
import time
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import InspectionRun, InspectionTask, SysSetting, TaskExecution, TaskExecutionCancel
from app.models.base import utcnow
from app.services import execution_runtime as runtime
from app.services import task_execution as queue
from app.workers.executions import _flush_pending_progress, _record_progress


@pytest.fixture()
def queued(mem_db):
    mem_db.add(InspectionTask(code="dcs_status", name="DCS", service="dcs", interval_sec=120,
                              enabled=True, params={"timeout_sec": 60}))
    mem_db.commit()
    result = queue.enqueue(mem_db, "dcs_status", request_key="request-1")
    return mem_db.get(TaskExecution, result["execution_ids"][0])


def control_for(row):
    return runtime.Control(row.id, row.run_id, row.owner_token, time.monotonic() + 60, row.params, lambda _: None)


def test_admission_deduplicates_manual_and_scheduled_scope(mem_db, queued):
    again = queue.enqueue(mem_db, "dcs_status", trigger="SCHEDULE")
    assert again["execution_ids"] == [queued.id] and again["deduplicated"]
    assert mem_db.scalar(select(func.count()).select_from(InspectionRun)) == 1
    claimed = queue.claim_next(mem_db, "worker-a")
    assert claimed.id == queued.id and claimed.status == "RUNNING"
    assert mem_db.get(InspectionRun, queued.run_id).status == "RUNNING"
    assert queue.claim_next(mem_db, "worker-b") is None


def test_historical_request_key_never_repeats_finished_execution(mem_db, queued):
    queue.finish(mem_db, queued, "SUCCESS")
    mem_db.commit()
    repeated = queue.enqueue(mem_db, "dcs_status", request_key="request-1")
    assert repeated["execution_ids"] == [queued.id] and repeated["deduplicated"]
    new = queue.enqueue(mem_db, "dcs_status", request_key="request-2")
    assert new["execution_ids"] != [queued.id]


def test_empty_scope_does_not_expand_to_all_targets(mem_db, queued):
    assert queue.enqueue(mem_db, "dcs_status", tenant_ids=[])["execution_ids"] == []
    assert queue.enqueue(mem_db, "dcs_status", project_refs=[])["execution_ids"] == []


def test_queued_cancel_finishes_both_records_without_claim(mem_db, queued):
    queue.request_cancel(mem_db, queued.id, "operator")
    assert queue.reap(mem_db) == 1
    assert queued.status == "CANCELLED" and queued.active_scope is None
    run = mem_db.get(InspectionRun, queued.run_id)
    assert run.status == "CANCELLED" and run.finished_at is not None
    assert queue.claim_next(mem_db, "worker-a") is None


def test_restart_recovery_preserves_live_lease_then_interrupts_expired_owner(mem_db, queued):
    row = queue.claim_next(mem_db, "worker-a")
    assert queue.reap(mem_db) == 0
    row.lease_expires_at = utcnow() - timedelta(seconds=1)
    mem_db.commit()
    assert queue.reap(mem_db) == 1
    assert row.status == "INTERRUPTED" and row.finished_at is not None
    assert queue.reap(mem_db) == 0
    new = queue.enqueue(mem_db, row.task_code, retry_of=row.id)
    assert new["execution_ids"] != [row.id]
    assert mem_db.get(InspectionRun, row.run_id).status == "INTERRUPTED"


def test_deadline_and_queue_expiry_have_distinct_reasons(mem_db, queued):
    queued.queue_expires_at = utcnow() - timedelta(seconds=1)
    mem_db.commit()
    assert queue.reap(mem_db) == 1
    assert queued.error_code == "LOCAL.QUEUE_EXPIRED"
    row = mem_db.get(TaskExecution, queue.enqueue(mem_db, "dcs_status")["execution_ids"][0])
    queue.claim_next(mem_db, "worker")
    row.deadline_at = utcnow() - timedelta(seconds=1)
    mem_db.commit()
    queue.reap(mem_db)
    assert row.status == "TIMEOUT" and row.error_code == "LOCAL.DEADLINE"


def test_legacy_recovery_dispatches_one_watch_for_each_stale_run(mem_db, queued, monkeypatch):
    from app.services import platform_watch

    now = utcnow()
    old = InspectionRun(task_code="mrs_job_status", status="RUNNING", started_at=now - timedelta(days=1))
    done = InspectionRun(task_code="mrs_job_status", status="SUCCESS", started_at=now - timedelta(days=1), finished_at=now)
    mem_db.add_all([old, done])
    mem_db.commit()
    observed = []
    monkeypatch.setattr(platform_watch, "_on_run", lambda db, run: observed.append((db, run.id, run.status)))
    queue.claim_next(mem_db, "live-worker")
    assert queue.reap(mem_db) == 0
    assert queue.reap(mem_db, legacy_before=now) == 1
    assert old.status == "INTERRUPTED" and done.status == "SUCCESS" and queued.status == "RUNNING"
    assert len(observed) == 1 and observed[0][0] is not mem_db
    assert observed[0][1:] == (old.id, "INTERRUPTED")


@pytest.mark.parametrize("stop", ["cancel", "lease", "owner", "deadline", "terminal"])
def test_commit_fence_rejects_stale_or_cancelled_writes(mem_db, queued, stop):
    row = queue.claim_next(mem_db, "worker-a")
    control = control_for(row)
    if stop == "cancel":
        mem_db.add(TaskExecutionCancel(execution_id=row.id, requested_by="operator"))
    elif stop == "lease":
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
    elif stop == "owner":
        row.owner_token = "different-owner"
    elif stop == "deadline":
        row.deadline_at = utcnow() - timedelta(seconds=1)
    else:
        queue.finish(mem_db, row, "INTERRUPTED")
    mem_db.commit()
    with runtime.managed(mem_db, control), pytest.raises(runtime.ExecutionStopped):
        mem_db.add(SysSetting(key="should-not-persist", value={"result": True}))
        mem_db.commit()
    mem_db.rollback()
    assert mem_db.get(SysSetting, "should-not-persist") is None


def test_result_and_execution_terminal_commit_together(mem_db, queued):
    row = queue.claim_next(mem_db, "worker-a")
    control = control_for(row)
    with runtime.managed(mem_db, control):
        run = runtime.new_run(mem_db, task_code=row.task_code, status="RUNNING")
        mem_db.add(SysSetting(key="saved-result", value={"complete": True}))
        run.status, run.finished_at, run.resource_total = "SUCCESS", utcnow(), 7
        mem_db.commit()
    assert control.done and row.status == "SUCCESS" and row.active_scope is None
    assert row.run_id == run.id and mem_db.get(SysSetting, "saved-result") is not None


def test_nested_savepoint_does_not_end_execution(mem_db, queued):
    row = queue.claim_next(mem_db, "worker-a")
    control = control_for(row)
    with runtime.managed(mem_db, control):
        with mem_db.begin_nested():
            mem_db.add(SysSetting(key="batch", value={"one": True}))
        assert not control.done and row.status == "RUNNING"
        mem_db.commit()
        assert not control.done


def test_same_phase_progress_counts_are_kept_in_history():
    from types import SimpleNamespace

    now = utcnow()
    row = SimpleNamespace(status="RUNNING", progress={"phase": "mrs_executions_page", "current": 1, "total": 4,
                                                     "unit": "page", "message": "query NEW"},
                          history=[{"phase": "mrs_executions_page", "current": 1, "total": 4,
                                    "unit": "page", "message": "query NEW"}])
    assert _record_progress(row, {"phase": "mrs_executions_page", "current": 2, "total": 4,
                                  "unit": "page", "message": "query NEW", "at": now.isoformat()}, now=now)
    assert row.progress["current"] == 2
    assert row.history[-1]["current"] == 2 and row.history[-1]["at"] == now.isoformat()


def test_progress_emits_each_real_count_change_within_the_same_phase():
    events = []
    control = runtime.Control("execution", 1, "owner", time.monotonic() + 10, {}, events.append)
    control.progress("mrs_executions_page", current=0, total=1, unit="page")
    control.progress("mrs_executions_page", current=1, total=1, unit="page")
    control.progress("mrs_executions_page", current=0, total=None, unit="page", message="读取另一查询")
    control.progress("mrs_executions_page", current=1, total=1, unit="page", message="另一查询完成")
    assert [(entry["current"], entry["total"]) for entry in events] == [
        (0, 1), (1, 1), (0, None), (1, 1)]


def test_progress_phase_transition_clears_previous_phase_counts_and_metadata():
    events = []
    control = runtime.Control("execution", 1, "owner", time.monotonic() + 10, {}, events.append)
    control.progress("mrs_collection_complete", current=3, total=3, unit="cluster", cluster_id="cluster-a",
                     api_calls=20, message="MRS 采集完成")
    control.progress("evaluating", current=0, total=6, unit="规则项", message="准备规则检查")

    assert events[-1]["phase"] == "evaluating"
    assert events[-1]["current"] == 0 and events[-1]["total"] == 6 and events[-1]["unit"] == "规则项"
    assert events[-1]["api_calls"] == 20 and events[-1]["message"] == "准备规则检查"
    assert "cluster_id" not in events[-1]

    control.progress("saving", current=6, total=6, unit="resource", message="保存巡检结果")

    assert events[-1]["phase"] == "saving"
    assert events[-1]["current"] == events[-1]["total"] == 6
    assert "cluster_id" not in events[-1]


def test_supervisor_persists_all_queued_page_progress_events_in_order():
    from collections import deque
    from types import SimpleNamespace

    now = utcnow()
    row = SimpleNamespace(id="execution", run_id=1, task_code="mrs_job_status", tenant_id=1, project_ref=1,
                          status="RUNNING", progress={"phase": "queued"}, history=[{"phase": "queued"}])
    pending = deque(({"phase": "mrs_executions_page", "current": page % 2, "total": 1,
                      "unit": "page", "query": f"query_{page}", "message": f"读取 query_{page}",
                      "at": now.isoformat()} for page in range(18)), maxlen=100)

    latest = _flush_pending_progress(row, pending, now=now)

    assert not pending
    assert latest["query"] == "query_17"
    assert [event["query"] for event in row.history[1:]] == [f"query_{page}" for page in range(18)]


def test_terminal_progress_is_ordered_before_finished_event():
    from types import SimpleNamespace

    finished = utcnow()
    saving = finished - timedelta(milliseconds=10)
    row = SimpleNamespace(status="SUCCESS", progress={"phase": "finished"}, history=[
        {"phase": "queued"}, {"phase": "finished", "at": finished.isoformat()},
    ])
    _record_progress(row, {"phase": "saving", "current": 3, "total": 3, "at": saving.isoformat()},
                     now=finished)
    assert [event["phase"] for event in row.history] == ["queued", "saving", "finished"]
    assert row.history[-2]["at"] < row.history[-1]["at"]
    row.progress = {**row.history[-2], "phase": "finished", "message": "SUCCESS"}
    assert row.progress["phase"] == "finished" and row.progress["current"] == 3


def test_managed_pipeline_uses_existing_run_and_finishes_exactly_once(mem_db, mock_http):
    from app.demo.seed import _tenants
    from app.services.pipeline import run_task
    from app.services.tenancy import resolve_targets

    db = mem_db
    db.add(InspectionTask(code="dcs_status", name="DCS", service="dcs", interval_sec=120, enabled=True, params={}))
    _tenants(db)
    db.commit()
    target = resolve_targets(db)[0]
    ids = queue.enqueue(db, "dcs_status", [target.tenant_id], project_refs=[target.project_ref])["execution_ids"]
    assert len(ids) == 1
    row = queue.claim_next(db, "worker-a")
    before = db.scalar(select(func.count()).select_from(InspectionRun))
    control = control_for(row)
    with runtime.managed(db, control):
        run = run_task(db, row.task_code, target=target, client=target.client(http=mock_http, mock_url="http://mock"))
    assert run.id == row.run_id
    assert row.status == run.status and row.status in ("SUCCESS", "PARTIAL")
    assert db.scalar(select(func.count()).select_from(InspectionRun)) == before
