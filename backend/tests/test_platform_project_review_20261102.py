"""Project-scoped platform runtime contracts; no production environment or services."""
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import metrics
from app.api.v1 import system
from app.core.deps import Scope
from app.models import AlarmEvent, InspectionRun, InspectionTask
from app.services import dashboard_service, platform_watch
from app.tools import status as status_tool

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
CODE = "project-runtime-review"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    for module in (metrics, system, status_tool, platform_watch):
        monkeypatch.setattr(module, "utcnow", lambda: NOW)


@pytest.fixture
def task(mem_db):
    row = InspectionTask(code=CODE, name="项目运行审计", service="cce", interval_sec=60)
    mem_db.add(row)
    mem_db.commit()
    return row


def run(db, status, minute, *, tenant=1, project=101, watch=True, code=CODE, error=None):
    row = InspectionRun(task_code=code, tenant_id=tenant, status=status, error_summary=error,
                        started_at=NOW + timedelta(minutes=minute), finished_at=NOW + timedelta(minutes=minute))
    row.project_ref = project
    db.add(row)
    db.commit()
    if watch:
        platform_watch.on_run(db, row)
    return row


def alarms(db, rule="R-PLAT-001"):
    return list(db.scalars(select(AlarmEvent).where(AlarmEvent.rule_id == rule)))


def failures(db, **kwargs):
    for minute in (-5, -4, -3):
        run(db, "FAILED", minute, **kwargs)
    return alarms(db)[-1]


def test_run_project_is_a_persisted_nullable_column():
    assert "project_ref" in InspectionRun.__table__.columns
    assert InspectionRun.__table__.columns.project_ref.nullable


@pytest.mark.parametrize("result", ["PARTIAL", "SKIPPED", "TIMEOUT"])
def test_non_success_does_not_resolve_an_existing_failure(task, mem_db, result):
    alarm = failures(mem_db)
    run(mem_db, result, -1)
    assert alarm.status == "FIRING"


def test_other_project_success_does_not_resolve_failure(task, mem_db):
    alarm = failures(mem_db)
    run(mem_db, "SUCCESS", -1, project=102)
    assert alarm.status == "FIRING"
    run(mem_db, "SUCCESS", 0)
    assert alarm.status == "RESOLVED"


def test_unknown_project_is_not_inferred_or_resolved_from_a_known_project(task, mem_db):
    alarm = failures(mem_db, project=None)
    run(mem_db, "SUCCESS", -1, project=101)
    assert alarm.status == "FIRING"
    run(mem_db, "SUCCESS", 0, project=None)
    assert alarm.status == "FIRING"
    unknown = next(s for s in platform_watch.task_scopes(mem_db) if s["project_ref"] is None)
    assert unknown["fails"] == 3


def test_timeout_partial_and_skipped_have_consistent_streak_semantics(task, mem_db):
    for minute, result in enumerate(("FAILED", "SKIPPED", "TIMEOUT", "PARTIAL"), -4):
        run(mem_db, result, minute)
    assert len(alarms(mem_db)) == 1 and alarms(mem_db)[0].detail["failures"] == 3


def test_failure_streak_does_not_include_another_project(task, mem_db):
    for minute, project in ((-4, 101), (-3, 102), (-2, 101)):
        run(mem_db, "FAILED", minute, project=project)
    assert alarms(mem_db) == []


def test_auth_recovery_is_project_scoped_and_success_only(task, mem_db):
    run(mem_db, "FAILED", -4, error="[403] denied")
    [alarm] = alarms(mem_db, "R-PLAT-002")
    run(mem_db, "SUCCESS", -3, project=102)
    run(mem_db, "PARTIAL", -2)
    assert alarm.status == "FIRING"
    run(mem_db, "SUCCESS", -1)
    assert alarm.status == "RESOLVED"


def test_success_of_another_task_does_not_prove_403_permission_recovery(task, mem_db):
    run(mem_db, "FAILED", -2, error="[403] service read permission denied")
    [alarm] = alarms(mem_db, "R-PLAT-002")
    run(mem_db, "SUCCESS", -1, code="another-service")
    assert alarm.status == "FIRING"


def test_old_success_callback_cannot_clear_a_later_failure(task, mem_db):
    old = run(mem_db, "SUCCESS", -10, watch=False)
    alarm = failures(mem_db)
    platform_watch.on_run(mem_db, old)
    assert alarm.status == "FIRING"


def test_status_has_every_tenant_project_not_just_latest_40_runs(task, mem_db):
    run(mem_db, "FAILED", -100, project=101, tenant=999, watch=False)
    for minute in range(-60, -10):
        run(mem_db, "SUCCESS", minute, tenant=1, project=102, watch=False)
    result = status_tool.collect(mem_db)["tasks"][0]
    assert len(result["tenants"]) == 2
    assert {(r["tenant_id"], r["project_ref"]) for r in result["tenants"]} == {(999, 101), (1, 102)}
    assert result["status"] == "WARN"


def test_metrics_have_project_identity_and_true_success_even_after_26_hours(task, mem_db, monkeypatch):
    successful = run(mem_db, "SUCCESS", -2000, watch=False)
    run(mem_db, "PARTIAL", -2, watch=False)
    run(mem_db, "SUCCESS", -1, project=102, watch=False)
    monkeypatch.setattr(metrics, "SessionLocal", lambda: nullcontext(mem_db))
    text = metrics.collect()
    rows = [s for s in text.splitlines() if s.startswith("hcs_inspector_task_last_success_timestamp_seconds{")]
    assert len(rows) == 2 and all('project_ref="' in row and 'tenant_id="1"' in row for row in rows)
    assert any('project_ref="101"' in row and row.endswith(f" {int(successful.finished_at.timestamp())}") for row in rows)


def test_platform_api_keeps_project_failures_and_skipped_evidence(task, mem_db):
    run(mem_db, "FAILED", -4, watch=False)
    run(mem_db, "SKIPPED", -2, watch=False)
    run(mem_db, "SUCCESS", -1, project=102, watch=False)
    data = system.platform(db=mem_db, scope=Scope(None, True, None))["data"]
    row = next(t for t in data["tasks"] if t["code"] == CODE)
    assert len(row["tenants"]) == 2 and row["status"] == "WARN"
    failed = next(p for p in row["tenants"] if p["project_ref"] == 101)
    assert failed["fails"] == 1 and failed["effective_status"] == "FAILED" and failed["last_status"] == "SKIPPED"


def test_task_history_limits_are_per_scope_and_tenant_filter_still_applies(task, mem_db):
    run(mem_db, "FAILED", -100, tenant=999, project=101, watch=False)
    for minute in range(-60, -10):
        run(mem_db, "SUCCESS", minute, project=102, watch=False)
    result = system.tasks(db=mem_db, scope=Scope(None, True, None))["data"][0]
    assert len(result["runs"]) == 13 and len(result["scopes"]) == 2
    result = system.tasks(db=mem_db, scope=Scope([999], False, None))["data"][0]
    assert len(result["runs"]) == 1 and result["runs"][0]["tenant_id"] == 999
    result = system.platform(db=mem_db, scope=Scope([999], False, None), project_ref=102)["data"]
    assert result["tasks"][0]["tenants"] == []


def test_unknown_project_filter_does_not_assign_a_project(task, mem_db):
    run(mem_db, "SUCCESS", -2, project=None, watch=False)
    run(mem_db, "SUCCESS", -1, watch=False)
    result = system.tasks(db=mem_db, scope=Scope(None, True, None), unknown_project=True)["data"][0]
    assert len(result["runs"]) == 1 and result["runs"][0]["project_ref"] is None
    assert result["scopes"][0]["project_scope"] == "UNKNOWN"


def test_unknown_project_filter_does_not_misclassify_global_cleanup(task, mem_db):
    mem_db.add(InspectionTask(code="data_cleanup", name="全局清理", service="platform", interval_sec=3600))
    mem_db.commit()
    run(mem_db, "SUCCESS", -2, project=None, watch=False)
    run(mem_db, "SUCCESS", -1, tenant=None, project=None, code="data_cleanup", watch=False)
    result = system.tasks(db=mem_db, scope=Scope(None, True, None), unknown_project=True)["data"]
    cleanup = next(t for t in result if t["code"] == "data_cleanup")
    assert cleanup["runs"] == [] and cleanup["scopes"] == []


def test_global_internal_cleanup_can_recover_without_a_project(mem_db):
    alarm = failures(mem_db, tenant=None, project=None, code="data_cleanup")
    run(mem_db, "SUCCESS", -1, tenant=None, project=None, code="data_cleanup")
    assert alarm.status == "RESOLVED"


def test_metrics_do_not_label_partial_or_missing_completion_as_success(task, mem_db, monkeypatch):
    run(mem_db, "PARTIAL", -3, watch=False)
    pending = run(mem_db, "SUCCESS", -2, project=102, watch=False)
    pending.finished_at = None
    mem_db.commit()
    monkeypatch.setattr(metrics, "SessionLocal", lambda: nullcontext(mem_db))
    assert not any(line.startswith("hcs_inspector_task_last_success_timestamp_seconds{")
                   for line in metrics.collect().splitlines())


def test_incomplete_success_record_cannot_mask_failed_scope(task, mem_db):
    alarm = failures(mem_db)
    incomplete = run(mem_db, "SUCCESS", -1, watch=False)
    incomplete.finished_at = None
    mem_db.commit()
    platform_watch.on_run(mem_db, incomplete)
    assert alarm.status == "FIRING"
    scopes = platform_watch.task_scopes(mem_db)
    assert scopes[0]["fails"] == 3 and platform_watch.task_last_status(scopes) == "PARTIAL"


def test_ledger_producer_persists_project_even_when_nothing_is_collected(mem_db):
    from app.ledger.sync import sync_ledger

    result = sync_ledger(mem_db, target=SimpleNamespace(tenant_id=1, project_ref=101), types=["not-requested"])
    mem_db.expire_all()
    assert mem_db.get(InspectionRun, result.id).project_ref == 101
    assert result.status == "SKIPPED"


def test_same_timestamp_callbacks_use_id_tiebreaker(task, mem_db):
    old = run(mem_db, "SUCCESS", 0, watch=False)
    for _ in range(3):
        run(mem_db, "FAILED", 0)
    [alarm] = alarms(mem_db)
    platform_watch.on_run(mem_db, old)
    assert alarm.status == "FIRING"


def test_delayed_failed_callback_does_not_reopen_after_real_recovery(task, mem_db):
    alarm = failures(mem_db)
    last_failure = mem_db.scalar(select(InspectionRun).where(InspectionRun.status == "FAILED")
                                 .order_by(InspectionRun.id.desc()))
    run(mem_db, "SUCCESS", -1)
    platform_watch.on_run(mem_db, last_failure)
    assert alarm.status == "RESOLVED" and len(alarms(mem_db)) == 1


def test_no_runs_or_disabled_task_is_not_labeled_healthy(task, mem_db):
    snapshot = status_tool.collect(mem_db)
    result = snapshot["tasks"][0]
    assert result["status"] == "UNKNOWN"
    assert "结论：正常" not in status_tool.render(snapshot)
    task.enabled = False
    mem_db.commit()
    assert status_tool.collect(mem_db)["tasks"][0]["status"] == "DISABLED"


def test_historical_project_filter_is_mutually_exclusive(task, mem_db):
    from app.core.response import BizError

    for endpoint in (system.tasks, system.platform):
        with pytest.raises(BizError):
            endpoint(db=mem_db, scope=Scope(None, True, None), project_ref=101, unknown_project=True)


def test_real_http_task_queries_keep_tenant_and_project_scope(task, mem_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.db import get_db
    from app.core.deps import CurrentUser, get_current_user

    run(mem_db, "FAILED", -2, tenant=1, project=101, watch=False)
    run(mem_db, "SUCCESS", -1, tenant=2, project=202, watch=False)
    app = FastAPI()
    app.include_router(system.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=1, username="project-reader", display_name="reader", permissions=["task:view"], tenant_ids=[1])
    with TestClient(app) as client:
        data = client.get("/api/v1/system/tasks", params={"project_ref": 101}).json()["data"][0]
        assert data["runs"][0]["project_ref"] == 101
        assert data["scopes"][0]["tenant_id"] == 1
        other = client.get("/api/v1/system/platform", params={"project_ref": 202}).json()["data"]
        assert other["tasks"][0]["tenants"] == []
        assert client.get("/api/v1/system/tasks", params={"project_ref": -1}).status_code == 422


def test_dashboard_uses_worst_scope_instead_of_latest_single_success(task, mem_db):
    run(mem_db, "FAILED", -2, watch=False)
    run(mem_db, "SUCCESS", -1, project=102, watch=False)
    row = next(t for t in dashboard_service.summary(mem_db, NOW)["tasks"] if t["code"] == CODE)
    assert row["last_status"] == "FAILED" and len(row["scopes"]) == 2


def test_dashboard_history_with_a_success_record_is_not_reported_as_never_run(task, mem_db):
    run(mem_db, "SUCCESS", -1, project=None, watch=False)
    row = next(t for t in dashboard_service.summary(mem_db, NOW)["tasks"] if t["code"] == CODE)
    assert row["last_status"] == "SUCCESS" and row["scope_verified"] is False
    assert row["scopes"][0]["project_scope"] == "UNKNOWN"


def test_dashboard_does_not_sum_possibly_duplicate_history_and_project_counts(task, mem_db):
    configured_projects(mem_db, [101])
    historical = run(mem_db, "SUCCESS", -3, project=None, watch=False)
    historical.resource_total = 16
    current = run(mem_db, "SUCCESS", -1, watch=False)
    current.resource_total = 16
    mem_db.commit()
    row = next(t for t in dashboard_service.summary(mem_db, NOW)["tasks"] if t["code"] == CODE)
    assert row["resource_total"] == 16
    assert row["count_basis"] == "LATEST_COMPLETE_KNOWN_PROJECTS"
    assert row["representative_scope"]["project_ref"] == 101
    assert row["history_unknown_scope_count"] == 1


def configured_projects(db, projects):
    from app.models import HcsAccount, HcsProject

    db.add(HcsAccount(id=1, code="runtime-counts", name="synthetic", iam_endpoint="https://iam.invalid",
                      domain_name="synthetic", auth_type="PASSWORD", secret_cipher="synthetic"))
    db.flush()
    for project in projects:
        db.add(HcsProject(id=project, account_id=1, region_id="synthetic", project_id=f"project-{project}",
                          external_domain="invalid", enabled=True))
    db.commit()


@pytest.mark.parametrize("known_counts,legacy_count,expected", [([34, 15], 49, 49), ([100, 96], 196, 196)])
def test_dashboard_counts_only_complete_known_project_runs(task, mem_db, known_counts, legacy_count, expected):
    configured_projects(mem_db, [101, 102])
    old = run(mem_db, "SUCCESS", -5, project=None, watch=False)
    old.resource_total = legacy_count
    for project, count in zip((101, 102), known_counts, strict=True):
        current = run(mem_db, "SUCCESS", -2 if project == 101 else -1, project=project, watch=False)
        current.resource_total = count
    mem_db.commit()
    row = next(t for t in dashboard_service.summary(mem_db, NOW)["tasks"] if t["code"] == CODE)
    assert row["resource_total"] == expected and row["counts_complete"] is True
    assert len(row["counted_scopes"]) == 2 and row["history_unknown_scope_count"] == 1


def test_skipped_after_success_keeps_known_count_without_claiming_a_new_success(task, mem_db):
    configured_projects(mem_db, [101, 102])
    for project, count in ((101, 34), (102, 15)):
        actual = run(mem_db, "SUCCESS", -3, project=project, watch=False)
        actual.resource_total = count
        mem_db.commit()
        run(mem_db, "SKIPPED", -1, project=project, watch=False)
    row = next(t for t in dashboard_service.summary(mem_db, NOW)["tasks"] if t["code"] == CODE)
    assert row["resource_total"] == 49 and row["counts_complete"] is True
    assert row["last_status"] == "SKIPPED"
    assert all(s["finished_at"] == NOW - timedelta(minutes=3) for s in row["counted_scopes"])


@pytest.mark.parametrize("second", [None, "PARTIAL", "FAILED", "SKIPPED"])
def test_dashboard_count_is_unknown_when_known_project_coverage_is_incomplete(task, mem_db, second):
    configured_projects(mem_db, [101, 102])
    first = run(mem_db, "SUCCESS", -2, watch=False)
    first.resource_total = 34
    if second is not None:
        other = run(mem_db, second, -1, project=102, watch=False)
        other.resource_total = 15
    mem_db.commit()
    row = next(t for t in dashboard_service.summary(mem_db, NOW)["tasks"] if t["code"] == CODE)
    assert row["resource_total"] is None and row["abnormal_total"] is None
    assert row["counts_complete"] is False


@pytest.mark.parametrize("statuses,expected", [
    ([], "SKIPPED"), (["SUCCESS"], "SUCCESS"), (["PARTIAL"], "PARTIAL"),
    (["TIMEOUT"], "FAILED"), (["FAILED", "SUCCESS"], "PARTIAL"),
    (["SUCCESS", "SKIPPED"], "PARTIAL"), (["SKIPPED"], "SKIPPED"),
])
def test_shared_manual_result_never_turns_partial_or_skip_into_success(statuses, expected):
    rows = [SimpleNamespace(status=s) for s in statuses]
    assert platform_watch.run_outcome(rows) == expected


def test_dashboard_pod_occurrences_are_window_counts_and_separate_projects(mem_db):
    from app.models import PodEvent

    for project, count, event_type, detail in [(101, 4, "RESTART", {}), (102, 2, "RESTART", {}),
                                               (101, 1, "OOM_KILLED", {"restart_counted": False})]:
        mem_db.add(PodEvent(tenant_id=1, project_ref=project, cluster_id="c", cluster_name="c", namespace="ns",
                           workload_kind="StatefulSet", workload="worker", pod="worker-0", event_type=event_type,
                           level="WARNING", source="POD_STATUS", count=count, detail=detail,
                           dedup_key=f"{project}-{event_type}", first_time=NOW, last_time=NOW))
    mem_db.commit()
    data = dashboard_service._pods(mem_db, NOW, [1])
    assert data["restarts_24h"] == 6 and data["oom_24h"] == 1
    assert len(data["top_restart"]) == 2 and {r["project_ref"] for r in data["top_restart"]} == {101, 102}


def test_dashboard_unknown_window_is_none_not_event_row_count(mem_db):
    from app.models import PodEvent

    mem_db.add(PodEvent(tenant_id=1, project_ref=101, cluster_id="c", cluster_name="c", namespace="ns",
                       workload_kind="Job", workload="worker", pod="worker", event_type="OOM_KILLED",
                       level="CRITICAL", source="K8S_EVENT", count=100, dedup_key="unknown-window",
                       first_time=NOW - timedelta(days=2), last_time=NOW))
    mem_db.commit()
    data = dashboard_service._pods(mem_db, NOW, [1])
    assert data["oom_24h"] is None and data["restarts_24h"] is None
    assert data["top_restart"][0]["count"] is None


@pytest.mark.parametrize("result", ["PARTIAL", "TIMEOUT", "SKIPPED"])
def test_manual_pod_endpoint_result_and_audit_do_not_claim_success(task, mem_db, monkeypatch, result):
    from app.api.v1 import pod_events
    from app.core.response import BizError

    task.code = "cce_pod_events"
    mem_db.commit()
    row = run(mem_db, result, -1, code=task.code, watch=False)
    monkeypatch.setattr(pod_events.pipeline, "run_all", lambda *_a, **_kw: [row])
    audits = []
    audit = SimpleNamespace(log=lambda *_a, **kw: audits.append(kw))
    def invoke():
        return pod_events.collect_now(db=mem_db, scope=Scope(None, True, None), audit=audit)
    if result == "TIMEOUT":
        with pytest.raises(BizError) as error:
            invoke()
        assert error.value.http_status == 502
    else:
        assert invoke()["data"]["status"] == result
    assert audits[0]["result"] != "SUCCESS"


@pytest.mark.parametrize("previous_status", ["PARTIAL", "TIMEOUT", "SKIPPED"])
def test_system_enqueue_audit_does_not_claim_a_completed_inspection(task, mem_db, monkeypatch, previous_status):
    from app.core.deps import CurrentUser
    from app.models import HcsAccount, HcsProject, TaskExecution

    task.code = "dcs_status"
    mem_db.add(HcsAccount(id=1, code="tenant", name="租户", iam_endpoint="https://example.invalid", domain_name="test"))
    mem_db.flush()
    mem_db.add(HcsProject(id=101, account_id=1, project_id="p", region_id="r", external_domain="example.invalid"))
    mem_db.commit()
    previous = run(mem_db, previous_status, -1, code=task.code, watch=False)
    audits = []
    audit = SimpleNamespace(log=lambda action, **kw: audits.append({"action": action, **kw}))
    user = CurrentUser(id=1, username="operator", display_name="operator", permissions=["task:run"])
    response = system.run_task(task.code, db=mem_db, scope=Scope([1], True, user), audit=audit, user=user)
    assert response["data"]["status"] == "QUEUED"
    execution = mem_db.get(TaskExecution, response["data"]["execution_ids"][0])
    assert execution.status == "QUEUED" and execution.finished_at is None
    assert previous.status == previous_status and previous.finished_at is not None
    assert audits[0]["action"] == "task.enqueue" and audits[0]["detail"]["outcome"] == "QUEUED"
    assert "runs" not in audits[0]["detail"]
