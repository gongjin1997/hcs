"""Report deep-link scope reaches Event, stats and PodState metadata; no upstream requests."""
from datetime import UTC, datetime

import pytest

from app.models import PodEvent, PodState
from app.models.pod import pod_event_key, pod_state_key

from .test_pod_scope_isolation import pod_api as pod_api  # noqa: F401

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


@pytest.fixture
def scoped_rows(mem_db, monkeypatch):
    monkeypatch.setattr("app.api.v1.pod_events.utcnow", lambda: NOW)
    for tenant, project, kind in [(1, 101, "Job"), (1, 101, "StatefulSet"),
                                   (1, 102, "Job"), (2, 201, "Job"), (1, None, "Job")]:
        pod = f"worker-{kind.lower()}"
        mem_db.add(PodState(key=pod_state_key(tenant, project, "same-cluster", "ns", pod),
            tenant_id=tenant, project_ref=project, cluster_id="same-cluster", cluster_name="same-cluster",
            namespace="ns", pod=pod, workload="worker", workload_kind=kind, phase="Running", ready=True,
            first_seen=NOW, last_seen=NOW))
        mem_db.add(PodEvent(tenant_id=tenant, project_ref=project, cluster_id="same-cluster", cluster_name="same-cluster",
            namespace="ns", pod=pod, workload="worker", workload_kind=kind, count=1, event_type="RESTART",
            source="POD_STATUS", level="WARNING", first_time=NOW, last_time=NOW,
            dedup_key=pod_event_key(tenant, project, "same-cluster", kind)))
    mem_db.commit()


def test_report_project_and_kind_scope_events_stats_and_pod_counts(pod_api, scoped_rows):
    client, _ = pod_api
    params = {"tenant_id": 1, "project_ref": 101, "cluster_id": "same-cluster", "namespace": "ns",
              "workload": "worker", "workload_kind": "Job"}
    events = client.get("/api/v1/pod-events", params=params).json()["data"]
    assert events["total"] == 1 and events["items"][0]["workload_kind"] == "Job"
    stats = client.get("/api/v1/pod-events/stats", params=params).json()["data"]
    assert stats["total"] == 1 and stats["restarts"] == 1 and stats["pods"]["total"] == 1
    assert stats["top_workloads"][0]["workload_kind"] == "Job"
    options = client.get("/api/v1/pod-events/options", params=params).json()["data"]
    assert {(r["tenant_id"], r["project_ref"]) for r in options["workloads"]} == {(1, 101)}


def test_explicit_unknown_project_does_not_include_known_projects(pod_api, scoped_rows):
    client, _ = pod_api
    params = {"tenant_id": 1, "unknown_project": True}
    events = client.get("/api/v1/pod-events", params=params).json()["data"]
    assert events["total"] == 1 and events["items"][0]["project_ref"] is None
    stats = client.get("/api/v1/pod-events/stats", params=params).json()["data"]
    assert stats["pods"]["total"] == 1
    options = client.get("/api/v1/pod-events/options", params=params).json()["data"]
    assert all(row["project_ref"] is None for row in options["workloads"])


def test_conflicting_project_selector_is_rejected(pod_api, scoped_rows):
    client, _ = pod_api
    for path in ("", "/stats", "/options"):
        response = client.get("/api/v1/pod-events" + path, params={"project_ref": 101, "unknown_project": True})
        assert response.status_code == 400


def test_query_tenant_id_cannot_expand_authorization(pod_api, scoped_rows):
    client, user = pod_api
    user.tenant_ids = [1]
    params = {"tenant_id": 2, "project_ref": 201, "workload_kind": "Job"}
    assert client.get("/api/v1/pod-events", params=params).json()["data"]["total"] == 0
    assert client.get("/api/v1/pod-events/stats", params=params).json()["data"]["pods"]["total"] == 0
    assert client.get("/api/v1/pod-events/options", params=params).json()["data"]["workloads"] == []
