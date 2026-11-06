"""Pod snapshot freshness is read-only, per Pod and per tenant/project/cluster."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.api.v1 import pod_events
from app.models import AlarmEvent, InspectionRun, InspectionTask, Notification, PodEvent, PodState
from app.models.pod import pod_event_key, pod_state_key
from app.services.pod_freshness import freshness_ttl, snapshot_freshness

from .test_pod_scope_isolation import pod_api as pod_api  # noqa: F401

NOW = datetime(2026, 11, 6, 3, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(pod_events, "utcnow", lambda: NOW)


def snapshot(db, *, tenant=1, project=11, cluster="same-cluster", name="worker-0", namespace="ns", age=0,
             ready=True, phase="Running", reason=None, restarts=7, observed=True, deleted=False, kind="StatefulSet"):
    state = PodState(key=pod_state_key(tenant, project, cluster, namespace, name), tenant_id=tenant, project_ref=project,
                     cluster_id=cluster, cluster_name=cluster, namespace=namespace, pod=name,
                     workload_kind=kind, workload="worker", phase=phase, ready=ready, restarts=restarts,
                     status_reason=reason, data={"status_observed": observed}, deleted=deleted,
                     last_seen=NOW - timedelta(seconds=age), containers=[], labels={})
    db.add(state)
    db.commit()
    return state


def stats(client, **params):
    response = client.get("/api/v1/pod-events/stats", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def timeline(client, state):
    response = client.get("/api/v1/pod-events/pod", params={"key": f"{state.cluster_id}/{state.namespace}/{state.pod}",
                                                          "tenant_id": state.tenant_id, "project_ref": state.project_ref})
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.parametrize("age,phase,observed,expected", [
    (0, "Running", True, "FRESH"), (900, "Running", True, "FRESH"), (901, "Running", True, "STALE"),
    (72 * 3600, "Running", True, "STALE"), (-30, "Running", True, "FRESH"), (-31, "Running", True, "UNKNOWN"),
    (0, "Unknown", True, "UNKNOWN"), (0, "", True, "UNKNOWN"), (0, "Invalid", True, "UNKNOWN"),
    (0, "Running", False, "UNKNOWN"), (72 * 3600, "Running", False, "UNKNOWN"),
])
def test_stats_and_detail_agree_on_snapshot_freshness(mem_db, pod_api, age, phase, observed, expected):
    client, _ = pod_api
    state = snapshot(mem_db, age=age, phase=phase, observed=observed, ready=False, reason="CrashLoopBackOff")
    result = stats(client)["pods"]
    assert (result["total"], result["fresh"], result["stale"], result["unknown"]) == (
        1, int(expected == "FRESH"), int(expected == "STALE"), int(expected == "UNKNOWN"))
    current = expected == "FRESH"
    assert result["ready"] == 0
    assert result["not_ready"] == result["crashloop"] == int(current)
    assert result["restarts"] == (7 if current else 0)
    [scope] = result["scopes"]
    detail = timeline(client, state)["state"]
    assert detail["freshness"]["status"] == scope["freshness"]["status"] == expected
    assert detail["freshness"]["stale_after_sec"] == scope["freshness"]["stale_after_sec"] == 900
    assert detail["freshness"]["age_sec"] == (None if age < -30 else max(age, 0))
    assert detail["restarts"] == 7 and detail["status_reason"] == "CrashLoopBackOff"


def test_72_hour_ready_snapshot_cannot_be_counted_as_current_ready(mem_db, pod_api):
    client, _ = pod_api
    old = snapshot(mem_db, age=72 * 3600)
    result = stats(client, hours=24)["pods"]
    assert (result["total"], result["ready"], result["not_ready"], result["crashloop"], result["fresh"], result["stale"]) == (
        1, 0, 0, 0, 0, 1)
    assert timeline(client, old)["state"]["ready"] is True  # Historical value remains available.
    mem_db.refresh(old)
    assert old.last_seen == NOW - timedelta(hours=72) and old.deleted is False


def test_newer_pod_cannot_freshen_an_older_pod_in_the_same_cluster(mem_db, pod_api):
    client, _ = pod_api
    snapshot(mem_db, age=72 * 3600, reason="CrashLoopBackOff", ready=False)
    snapshot(mem_db, name="worker-1", age=5)
    result = stats(client)["pods"]
    assert (result["total"], result["fresh"], result["stale"], result["ready"], result["crashloop"]) == (2, 1, 1, 1, 0)
    [scope] = result["scopes"]
    assert scope["freshness"]["status"] == "PARTIAL"
    assert scope["freshness"]["age_sec"] == 5 and scope["stale"] == 1


@pytest.mark.parametrize("interval,cron,ttl", [(60, None, 900), (600, None, 1800),
                                               (60, "0 */2 * * *", 21600), (60, "0 3 * * *", 259200)])
def test_existing_interval_and_cron_validity_rules_are_reused(mem_db, pod_api, interval, cron, ttl):
    client, _ = pod_api
    mem_db.add(InspectionTask(code="cce_pod_events", name="synthetic", service="cce", interval_sec=interval, cron=cron))
    mem_db.commit()
    snapshot(mem_db, age=ttl)
    snapshot(mem_db, name="worker-1", age=ttl + 1)
    result = stats(client)["pods"]
    assert (result["fresh"], result["stale"], result["ready"]) == (1, 1, 1)
    assert result["scopes"][0]["freshness"]["stale_after_sec"] == ttl


def test_invalid_schedule_is_unknown_instead_of_breaking_history_queries(mem_db, pod_api):
    client, _ = pod_api
    mem_db.add(InspectionTask(code="cce_pod_events", name="synthetic", service="cce", interval_sec=60, cron="invalid"))
    mem_db.commit()
    state = snapshot(mem_db)
    assert freshness_ttl(mem_db, NOW) is None
    result = stats(client)["pods"]
    assert result["fresh"] == result["ready"] == 0 and result["unknown"] == 1
    assert timeline(client, state)["state"]["freshness"]["status"] == "UNKNOWN"


def test_no_snapshot_and_deleted_snapshot_are_distinct_from_current_healthy(mem_db, pod_api):
    client, _ = pod_api
    empty = stats(client)["pods"]
    assert empty["total"] == empty["fresh"] == empty["ready"] == 0 and empty["scopes"] == []
    state = snapshot(mem_db, deleted=True)
    assert stats(client)["pods"] == empty
    assert timeline(client, state)["state"]["freshness"]["status"] == "DELETED"


@pytest.mark.parametrize("value", [None, datetime(2026, 11, 6, 3)])
def test_missing_or_timezone_unknown_last_seen_is_never_current(value):
    state = PodState(last_seen=value, phase="Running", ready=True, data={})
    result = snapshot_freshness(state, NOW, 900)
    assert result.status == "UNKNOWN" and result.last_observed_at is None and result.age_sec is None


def test_legacy_phase_evidence_remains_readable_without_new_metadata(mem_db, pod_api):
    client, _ = pod_api
    state = snapshot(mem_db)
    state.data = {}
    mem_db.commit()
    assert stats(client)["pods"]["fresh"] == 1
    assert timeline(client, state)["state"]["freshness"]["status"] == "FRESH"


def test_freshness_is_independent_for_tenant_project_cluster_and_legacy_scope(mem_db, pod_api):
    client, user = pod_api
    snapshot(mem_db)
    snapshot(mem_db, project=12, age=3600)
    snapshot(mem_db, tenant=2, project=21, age=7200)
    snapshot(mem_db, project=None, observed=False)
    snapshot(mem_db, cluster="other-cluster", age=10800)
    result = stats(client)["pods"]
    assert (result["total"], result["fresh"], result["stale"], result["unknown"], result["ready"]) == (5, 1, 3, 1, 1)
    scopes = {(s["tenant_id"], s["project_ref"], s["cluster_id"]): s for s in result["scopes"]}
    assert len(scopes) == 5
    assert scopes[(1, 11, "same-cluster")]["freshness"]["age_sec"] == 0
    assert scopes[(1, 12, "same-cluster")]["freshness"]["age_sec"] == 3600
    assert scopes[(2, 21, "same-cluster")]["freshness"]["age_sec"] == 7200
    assert scopes[(1, None, "same-cluster")]["freshness"]["last_observed_at"] is None
    assert scopes[(1, 11, "other-cluster")]["freshness"]["age_sec"] == 10800
    assert stats(client, tenant_id=1, project_ref=12)["pods"]["fresh"] == 0
    assert stats(client, tenant_id=1, unknown_project=True)["pods"]["unknown"] == 1
    assert stats(client, tenant_id=1, project_ref=11, cluster_id="same-cluster")["pods"]["fresh"] == 1
    user.tenant_ids = [1]
    restricted = stats(client)["pods"]
    assert restricted["total"] == 4 and all(s["tenant_id"] == 1 for s in restricted["scopes"])


def test_recent_task_run_cannot_refresh_unobserved_cluster_or_project(mem_db, pod_api):
    client, _ = pod_api
    snapshot(mem_db, age=3600)
    for project in (11, 12):
        mem_db.add(InspectionRun(task_code="cce_pod_events", tenant_id=1, project_ref=project, status="SUCCESS",
                                started_at=NOW, finished_at=NOW))
    mem_db.commit()
    result = stats(client)["pods"]
    assert result["fresh"] == 0 and result["stale"] == 1 and result["ready"] == 0


def test_namespace_and_workload_filters_do_not_borrow_fresh_snapshots(mem_db, pod_api):
    client, _ = pod_api
    snapshot(mem_db, age=3600, kind="Job")
    snapshot(mem_db, name="worker-1", namespace="other", kind="StatefulSet")
    assert stats(client, namespace="ns")["pods"]["stale"] == 1
    assert stats(client, workload="worker", workload_kind="Job")["pods"]["fresh"] == 0
    assert stats(client, workload="worker", workload_kind="StatefulSet")["pods"]["fresh"] == 1


def test_successful_new_snapshot_becomes_fresh_without_modifying_other_scope(mem_db, pod_api):
    client, _ = pod_api
    own = snapshot(mem_db, age=3600)
    snapshot(mem_db, project=12, age=3600)
    assert stats(client)["pods"]["fresh"] == 0
    own.last_seen = NOW
    own.data = {"status_observed": True}
    mem_db.commit()
    result = stats(client)["pods"]
    assert (result["fresh"], result["stale"], result["ready"]) == (1, 1, 1)


def test_freshness_queries_preserve_history_existing_alarms_and_snapshot_payload(mem_db, pod_api):
    client, _ = pod_api
    state = snapshot(mem_db, age=72 * 3600)
    event = PodEvent(tenant_id=1, project_ref=11, cluster_id="same-cluster", cluster_name="same-cluster",
                     namespace="ns", pod="worker-0", workload_kind="StatefulSet", workload="worker",
                     event_type="CRASH_LOOP", source="POD_STATUS", level="CRITICAL", ongoing=True,
                     dedup_key=pod_event_key(1, 11, "same-cluster", "synthetic"),
                     first_time=NOW - timedelta(hours=72), last_time=NOW - timedelta(hours=72))
    alarm = AlarmEvent(tenant_id=1, fingerprint="synthetic", rule_id="R-CCE-POD-003", service="cce",
                       resource_type="cce_workload", resource_name="worker", severity="P2", status="FIRING",
                       title="synthetic", check_item="pod_crashloop")
    mem_db.add_all([event, alarm])
    mem_db.commit()
    before = deepcopy((state.data, state.last_seen, state.ready, event.detail, event.ongoing, event.last_time))
    for _ in range(2):
        assert stats(client)["pods"]["stale"] == 1
        assert timeline(client, state)["events"]
    mem_db.refresh(state)
    mem_db.refresh(event)
    mem_db.refresh(alarm)
    assert before == (state.data, state.last_seen, state.ready, event.detail, event.ongoing, event.last_time)
    assert alarm.status == "FIRING" and alarm.resolved_at is None
    assert len(list(mem_db.scalars(select(AlarmEvent)))) == 1
    assert list(mem_db.scalars(select(Notification))) == []
