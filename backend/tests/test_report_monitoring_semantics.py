"""报表区间、真实执行分母和未知容量；使用隔离库。"""
from datetime import UTC, datetime, timedelta

from app.k8s.semantics import occurrence_interval
from app.models import AlarmEvent, AlarmEventLog, InspectionRun, PodEvent, Resource
from app.reports import builder
from app.services.capacity_summary import capacity_totals

END = datetime(2026, 11, 2, 12, tzinfo=UTC)
START = END - timedelta(days=1)


def test_skipped_and_running_tasks_do_not_lower_completed_success_rate(mem_db):
    for status in ("SUCCESS", "SKIPPED", "RUNNING"):
        mem_db.add(InspectionRun(task_code="test", status=status, started_at=START + timedelta(hours=1),
            finished_at=None if status == "RUNNING" else START + timedelta(hours=1, seconds=10)))
    mem_db.commit()
    result = builder.build_report(mem_db, "DAILY", START, END)
    assert result["overview"]["run_success_rate"] == 100
    assert result["tasks"][0]["success_rate"] == 100
    assert result["tasks"][0]["skipped"] == 1
    assert result["tasks"][0]["running"] == 1


def test_closing_after_period_does_not_erase_period_end_alarm(mem_db):
    alarm = AlarmEvent(fingerprint="close-after", rule_id="r", service="ecs", resource_type="ecs_server",
        resource_name="host", severity="P2", status="CLOSED", title="error", check_item="status",
        first_seen=START + timedelta(hours=1), last_seen=END, resolved_at=END + timedelta(hours=1))
    mem_db.add(alarm)
    mem_db.commit()
    data = builder.build_report(mem_db, "DAILY", START, END)
    assert data["overview"]["active_at_end"] == 1
    assert data["key_alarms"][0]["duration_min"] == 23 * 60


def test_reopen_after_period_does_not_reopen_historical_period(mem_db):
    alarm = AlarmEvent(fingerprint="reopen-after", rule_id="r", service="ecs", resource_type="ecs_server",
        resource_name="host", severity="P2", status="FIRING", title="error", check_item="status",
        first_seen=START + timedelta(hours=1), last_seen=END + timedelta(hours=1), resolved_at=None)
    mem_db.add(alarm)
    mem_db.flush()
    mem_db.add_all([
        AlarmEventLog(alarm_id=alarm.id, action="FIRE", to_status="FIRING", at=alarm.first_seen),
        AlarmEventLog(alarm_id=alarm.id, action="RESOLVE", to_status="RESOLVED", at=END - timedelta(hours=1)),
        AlarmEventLog(alarm_id=alarm.id, action="REOPEN", to_status="FIRING", at=END + timedelta(hours=1)),
    ])
    mem_db.commit()
    assert builder.build_report(mem_db, "DAILY", START, END)["overview"]["active_at_end"] == 0


def test_capacity_aggregate_distinguishes_empty_unknown_and_zero():
    assert capacity_totals([])["storage_gb"] == 0
    rows = [Resource(service="evs", resource_type="evs_volume", resource_id="v", name="v", storage_gb=None)]
    result = capacity_totals(rows)
    assert result["storage_gb"] is None and result["coverage"]["storage_gb"] == {"known": 0, "total": 1}
    rows.append(Resource(service="evs", resource_type="evs_volume", resource_id="zero", name="zero", storage_gb=0))
    result = capacity_totals(rows)
    assert result["storage_gb"] == 0 and result["coverage"]["storage_gb"] == {"known": 1, "total": 2}


def pod_event(db, *, tenant=1, kind="RESTART", count=3, first=None, last=None, detail=None, workload_kind="Deployment"):
    row = PodEvent(tenant_id=tenant, project_ref=tenant, cluster_id="same", cluster_name="cluster", namespace="ns",
        workload_kind=workload_kind, workload="app", pod="app-pod", container="container", event_type=kind,
        level="WARNING", source="POD_STATUS", count=count, first_time=first or START + timedelta(hours=1),
        last_time=last or START + timedelta(hours=2), dedup_key=f"{tenant}:{kind}:{workload_kind}", detail=detail or {})
    db.add(row)
    db.flush()
    return row


def test_report_counts_real_restarts_and_oom_without_counting_a_nonrestart_termination(mem_db):
    pod_event(mem_db)
    pod_event(mem_db, kind="OOM_KILLED", count=1, detail={"restart_counted": False})
    result = builder._pod_section(mem_db, START, END, None)
    assert result["restarts"] == 3 and result["oom"] == 1
    assert result["events"] == 2
    assert result["top_restart"][0]["count"] == 3


def test_report_cannot_place_cumulative_counts_across_period_boundaries(mem_db):
    pod_event(mem_db, first=START - timedelta(hours=1), last=START + timedelta(hours=1))
    assert builder._pod_section(mem_db, START, END, None)["restarts"] is None


def test_report_excludes_known_increments_after_period_and_keeps_scope_identity(mem_db):
    detail = {"count_intervals": [occurrence_interval(START, START, 2), occurrence_interval(END, END, 3)]}
    pod_event(mem_db, count=5, first=START, last=END, detail=detail)
    pod_event(mem_db, tenant=2)
    pod_event(mem_db, workload_kind="StatefulSet")
    result = builder._pod_section(mem_db, START, END, None)
    assert result["restarts"] == 8
    assert len(result["top_restart"]) == 3
    assert {row["workload_kind"] for row in result["top_restart"]} == {"Deployment", "StatefulSet"}
    assert builder._pod_section(mem_db, START, END, [1])["restarts"] == 5
