"""同 cid / Pod / Event UID 的租户、项目碰撞回归；不连接真实集群。"""
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.v1.pod_events import router
from app.collectors.base import RunContext
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.core.response import BizError
from app.k8s import collector
from app.models import PodEvent, PodProbe, PodState
from app.models.pod import SCOPE_META, pod_event_key, pod_state_key

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
KEY = "same-cluster/ns/same-pod"


class Cloud:
    def __init__(self, *, pods=True, crash=False, restarts=1, uid="same-event", pressure=0):
        self.present, self.crash, self.restarts, self.uid, self.pressure = pods, crash, restarts, uid, pressure

    def get(self, service, path, **_kwargs):
        if service == "cce":
            return {"items": [{"metadata": {"uid": "same-cluster", "name": "same-name"},
                               "status": {"phase": "Available"}}]}
        if path == "/api/v1/pods":
            state = {"waiting": {"reason": "CrashLoopBackOff"}} if self.crash else {"running": {"startedAt": NOW.isoformat()}}
            pods = [{
                "metadata": {"namespace": "ns", "name": "same-pod", "labels": {"app": "worker"},
                             "ownerReferences": [{"kind": "StatefulSet", "name": "worker", "controller": True}]},
                "spec": {"nodeName": "node", "containers": [{"name": "main", "resources": {}}]},
                "status": {"phase": "Running", "containerStatuses": [{
                    "name": "main", "restartCount": self.restarts, "ready": not self.crash, "state": state,
                    "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137,
                                                "finishedAt": (NOW - timedelta(minutes=1)).isoformat()}},
                }]},
            }] if self.present else []
            return {"items": deepcopy(pods), "metadata": {}}
        if path == "/api/v1/events":
            return {"items": [{
                "metadata": {"uid": self.uid, "namespace": "ns"},
                "involvedObject": {"kind": "Pod", "namespace": "ns", "name": "same-pod"},
                "type": "Warning", "reason": "Unhealthy", "message": "readiness probe failed",
                "firstTimestamp": NOW.isoformat(), "lastTimestamp": NOW.isoformat(), "count": 1,
            }] if self.uid else [], "metadata": {}}
        raise AssertionError("unexpected mock request")

    def get_text(self, *_a, **_kw):
        return "test_pressure " + str(self.pressure)


def collect(db, tenant, project, cloud=None):
    context = RunContext(client=cloud or Cloud(), account=str(tenant), project_id=str(project),
                         tenant_id=tenant, project_ref=project, db=db)
    observations = collector.PodEventCollector().collect(context)
    db.commit()
    return observations


def state(db, tenant, project):
    return db.scalar(select(PodState).where(PodState.tenant_id == tenant, PodState.project_ref == project))


def events(db, tenant, project):
    return list(db.scalars(select(PodEvent).where(PodEvent.tenant_id == tenant, PodEvent.project_ref == project)))


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(collector, "utcnow", lambda: NOW)
    monkeypatch.setattr("app.api.v1.pod_events.utcnow", lambda: NOW)


def test_three_scopes_with_same_cluster_pod_and_event_uid_do_not_overwrite_or_dedupe(mem_db):
    identities = [(101, 1001), (202, 2002), (101, 1002)]
    for number, (tenant, project) in enumerate(identities, 1):
        collect(mem_db, tenant, project, Cloud(restarts=number))
    rows = mem_db.scalars(select(PodState)).all()
    assert len(rows) == 3 and len({row.key for row in rows}) == 3
    for number, (tenant, project) in enumerate(identities, 1):
        row = state(mem_db, tenant, project)
        assert row.key == pod_state_key(tenant, project, "same-cluster", "ns", "same-pod")
        assert row.restarts == number and not row.deleted
        assert len(events(mem_db, tenant, project)) == 2
    before = {row.id: (row.count, row.message) for row in mem_db.scalars(select(PodEvent))}
    collect(mem_db, 101, 1001)
    assert {row.id: (row.count, row.message) for row in mem_db.scalars(select(PodEvent))} == before


def test_removal_and_crash_recovery_only_close_current_scope(mem_db):
    for tenant, project in ((101, 1001), (101, 1002), (202, 2002)):
        collect(mem_db, tenant, project, Cloud(crash=True))
    collect(mem_db, 101, 1001, Cloud(pods=False, uid=""))
    assert state(mem_db, 101, 1001).deleted
    assert not any(event.ongoing for event in events(mem_db, 101, 1001))
    for tenant, project in ((101, 1002), (202, 2002)):
        assert not state(mem_db, tenant, project).deleted
        assert any(event.ongoing for event in events(mem_db, tenant, project))
    collect(mem_db, 101, 1002, Cloud())
    assert not any(event.ongoing for event in events(mem_db, 101, 1002))
    assert any(event.ongoing for event in events(mem_db, 202, 2002))


def test_observation_counts_and_restart_tracking_ignore_other_projects(mem_db):
    collect(mem_db, 101, 1001, Cloud(restarts=10, crash=True))
    observed = collect(mem_db, 101, 1002, Cloud(restarts=1))
    current = next(o for o in observed if o.check_item == "pod_restart")
    assert current.metrics["pods"] == 1 and current.metrics["restarts_24h"] == 1
    assert current.metrics["crashloop_pods"] == 0
    again = collect(mem_db, 101, 1002, Cloud(restarts=2))
    assert next(o for o in again if o.check_item == "pod_restart").metrics["restarts_24h"] == 2
    assert state(mem_db, 101, 1001).restarts == 10


def test_probe_state_and_ongoing_event_are_separate_per_project(mem_db):
    probe = PodProbe(tenant_id=101, name="pressure", namespace="ns", metric="test_pressure", threshold=1,
                     for_minutes=1, recover_minutes=1)
    mem_db.add(probe)
    mem_db.commit()
    for project in (1001, 1002):
        collect(mem_db, 101, project, Cloud(pressure=2))
        row = state(mem_db, 101, project)
        row.data = {**row.data, f"bp{probe.id}": {"over_since": (NOW - timedelta(minutes=2)).isoformat()}}
        mem_db.commit()
        collect(mem_db, 101, project, Cloud(pressure=2))
    assert all(any(event.source == "PROBE" and event.ongoing for event in events(mem_db, 101, p)) for p in (1001, 1002))
    own = state(mem_db, 101, 1001)
    own.data = {**own.data, f"bp{probe.id}": {"under_since": (NOW - timedelta(minutes=2)).isoformat()}}
    mem_db.commit()
    collect(mem_db, 101, 1001, Cloud(pressure=0))
    assert not any(event.source == "PROBE" and event.ongoing for event in events(mem_db, 101, 1001))
    assert any(event.source == "PROBE" and event.ongoing for event in events(mem_db, 101, 1002))


def test_unassigned_legacy_state_is_not_adopted_by_known_project(mem_db):
    collect(mem_db, 101, None, Cloud(crash=True))
    legacy_key = state(mem_db, 101, None).key
    collect(mem_db, 101, 1001)
    assert state(mem_db, 101, None).key == legacy_key
    assert state(mem_db, 101, None).status_reason == "CrashLoopBackOff"
    assert state(mem_db, 101, 1001).status_reason is None


def test_finish_cleanup_is_bounded_by_tenant_project_and_visited_cluster(mem_db):
    for tenant, project, cid in ((101, 1001, "same-cluster"), (101, 1002, "same-cluster"),
                                 (202, 2002, "same-cluster"), (101, 1001, "not-visited")):
        mem_db.add(PodState(key=pod_state_key(tenant, project, cid, "old", "old"), tenant_id=tenant,
                            project_ref=project, cluster_id=cid, cluster_name="cluster", namespace="old", pod="old",
                            deleted=True, last_seen=NOW - timedelta(days=60)))
        mem_db.add(PodEvent(tenant_id=tenant, project_ref=project, cluster_id=cid, cluster_name="cluster",
                            namespace="old", pod="old", event_type="RESTART", level="WARNING", source="POD_STATUS",
                            dedup_key=pod_event_key(tenant, project, cid, "old"), first_time=NOW - timedelta(days=60),
                            last_time=NOW - timedelta(days=60)))
    mem_db.commit()
    collect(mem_db, 101, 1001)
    for tenant, project, cid in ((101, 1002, "same-cluster"), (202, 2002, "same-cluster"), (101, 1001, "not-visited")):
        assert mem_db.get(PodState, pod_state_key(tenant, project, cid, "old", "old")) is not None
        assert mem_db.scalar(select(PodEvent).where(PodEvent.tenant_id == tenant, PodEvent.project_ref == project,
                                                  PodEvent.cluster_id == cid, PodEvent.namespace == "old")) is not None
    assert mem_db.get(PodState, pod_state_key(101, 1001, "same-cluster", "old", "old")) is None


@pytest.fixture()
def pod_api(mem_db):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    user = CurrentUser(id=1, username="scope-review", display_name="scope-review",
                       permissions=["podevent:view"], tenant_ids=None)
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: user
    @app.exception_handler(BizError)
    async def biz(_request, error):
        return JSONResponse(status_code=error.http_status,
                            content={"code": error.code, "message": error.message, "data": error.data})
    with TestClient(app) as client:
        yield client, user


def test_event_id_exactly_locates_state_and_events_and_keeps_public_key(mem_db, pod_api):
    client, _ = pod_api
    for tenant, project in ((101, 1001), (101, 1002), (202, 2002)):
        collect(mem_db, tenant, project)
    for tenant, project in ((101, 1001), (101, 1002), (202, 2002)):
        anchor = events(mem_db, tenant, project)[0]
        response = client.get("/api/v1/pod-events/pod", params={"key": KEY, "event_id": anchor.id})
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["state"]["key"] == KEY and data["state"]["project_ref"] == project
        assert {(e["tenant_id"], e["project_ref"]) for e in data["events"]} == {(tenant, project)}
        assert all(SCOPE_META not in e["detail"] for e in data["events"])
    assert client.get("/api/v1/pod-events/pod", params={"key": KEY}).status_code == 409
    assert client.get("/api/v1/pod-events/pod", params={"key": KEY, "tenant_id": 101}).status_code == 409
    assert client.get("/api/v1/pod-events/pod", params={"key": KEY, "tenant_id": 101, "project_ref": 1001}).status_code == 200


def test_event_id_null_project_is_exact_and_never_falls_back(mem_db, pod_api):
    client, _ = pod_api
    collect(mem_db, 101, None)
    anchor = events(mem_db, 101, None)[0]
    collect(mem_db, 101, 1001)
    response = client.get("/api/v1/pod-events/pod", params={"key": KEY, "event_id": anchor.id})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"]["project_ref"] is None and all(e["project_ref"] is None for e in data["events"])
    mem_db.delete(state(mem_db, 101, None))
    mem_db.commit()
    data = client.get("/api/v1/pod-events/pod", params={"key": KEY, "event_id": anchor.id}).json()["data"]
    assert data["state"] is None


def test_timeline_404_for_missing_forbidden_or_mismatching_event(mem_db, pod_api):
    client, user = pod_api
    collect(mem_db, 101, 1001)
    anchor = events(mem_db, 101, 1001)[0]
    for params in ({"key": KEY, "event_id": 999999}, {"key": "different/ns/pod", "event_id": anchor.id},
                   {"key": KEY, "event_id": anchor.id, "tenant_id": 202},
                   {"key": KEY, "event_id": anchor.id, "project_ref": 1002}):
        assert client.get("/api/v1/pod-events/pod", params=params).status_code == 404
    user.tenant_ids = [202]
    assert client.get("/api/v1/pod-events/pod", params={"key": KEY, "event_id": anchor.id}).status_code == 404
    assert client.get("/api/v1/pod-events/pod", params={"key": KEY}).status_code == 404


def test_unambiguous_legacy_key_remains_compatible(mem_db, pod_api):
    client, _ = pod_api
    collect(mem_db, 101, 1001)
    response = client.get("/api/v1/pod-events/pod", params={"key": KEY})
    assert response.status_code == 200 and response.json()["data"]["state"]["key"] == KEY
    assert client.get("/api/v1/pod-events/pod", params={"key": "bad"}).status_code == 400


def test_stats_options_and_filters_do_not_merge_same_named_clusters(mem_db, pod_api):
    client, user = pod_api
    for tenant, project in ((101, 1001), (101, 1002), (202, 2002)):
        collect(mem_db, tenant, project)
    data = client.get("/api/v1/pod-events/stats").json()["data"]
    assert data["pods"]["clusters"] == 3 and len(data["top_workloads"]) == 3
    assert {w["project_ref"] for w in data["top_workloads"]} == {1001, 1002, 2002}
    options = client.get("/api/v1/pod-events/options").json()["data"]
    assert len(options["clusters"]) == 3
    assert {c["project_ref"] for c in options["clusters"]} == {1001, 1002, 2002}
    user.tenant_ids = [101]
    options = client.get("/api/v1/pod-events/options").json()["data"]
    assert len(options["clusters"]) == 2 and {c["tenant_id"] for c in options["clusters"]} == {101}
    data = client.get("/api/v1/pod-events", params={"project_ref": 1002}).json()["data"]
    assert data["items"] and all(e["project_ref"] == 1002 for e in data["items"])
    data = client.get("/api/v1/pod-events", params={"tenant_id": 202}).json()["data"]
    assert data["items"] == []


def test_long_public_pod_names_have_bounded_database_keys(mem_db, monkeypatch):
    long_name = "p" * 253
    original = Cloud.get
    def get(self, service, path, **kwargs):
        data = original(self, service, path, **kwargs)
        if path == "/api/v1/pods":
            data["items"][0]["metadata"]["name"] = long_name
        if path == "/api/v1/events":
            data["items"][0]["involvedObject"]["name"] = long_name
        return data
    monkeypatch.setattr(Cloud, "get", get)
    collect(mem_db, 101, 1001, Cloud(crash=True))
    row = state(mem_db, 101, 1001)
    assert len(row.key) < 160
    assert all(len(e.dedup_key) < 160 for e in events(mem_db, 101, 1001))


def test_cluster_options_namespaces_are_scoped_sorted_and_include_bare_pods(mem_db, pod_api):
    client, user = pod_api
    for tenant, project, namespaces in ((101, 1001, ("z-own", "a-own")), (101, 1002, ("other-project",)),
                                         (202, 2002, ("other-tenant",))):
        for namespace in namespaces:
            mem_db.add(PodState(key=pod_state_key(tenant, project, "same-cluster", namespace, "bare"),
                                tenant_id=tenant, project_ref=project, cluster_id="same-cluster", cluster_name="same-name",
                                namespace=namespace, pod="bare"))
    mem_db.commit()
    data = client.get("/api/v1/pod-events/options").json()["data"]
    assert {(c["tenant_id"], c["project_ref"]): c["namespaces"] for c in data["clusters"]} == {
        (101, 1001): ["a-own", "z-own"], (101, 1002): ["other-project"], (202, 2002): ["other-tenant"]}
    assert data["workloads"] == []
    user.tenant_ids = [101]
    data = client.get("/api/v1/pod-events/options").json()["data"]
    assert data["namespaces_by_cluster"] == {"same-cluster": ["a-own", "other-project", "z-own"]}
    assert all("other-tenant" not in c["namespaces"] for c in data["clusters"])
