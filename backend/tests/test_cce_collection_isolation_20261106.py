"""P1-1/P1-2: independent Pod evidence and scoped Event gaps, synthetic APIs only."""
import logging
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.collectors.base import RunContext
from app.hcs import HcsError
from app.k8s import collector
from app.models import AlarmEvent, Notification, PodEvent, PodState, Resource

from .test_cce_container_review_20261102 import CL, NOW, Client, event, pod, terminated
from .test_cce_event_diagnostics import collect_task as collect_task  # noqa: F401


def scoped_pod(name="alpha-0", uid="uid-a", *, namespace="ns", workload="alpha", kind="StatefulSet", **kwargs):
    item = pod(uid, owner=[{"kind": kind, "name": workload, "controller": True}], **kwargs)
    item["metadata"].update(name=name, namespace=namespace)
    return item


def scoped_event(uid="event-a", count=1, *, name="alpha-0", pod_uid="uid-a", namespace="ns", **kwargs):
    item = event(uid, count, name=name, pod_uid=pod_uid, **kwargs)
    item["metadata"]["namespace"] = namespace
    item["involvedObject"]["namespace"] = namespace
    return item


class EventFailure(Client):
    def __init__(self, pods, status=503):
        super().__init__(pods)
        self.status = status
        self.failure = HcsError(status, "TEST.UNAVAILABLE", "synthetic-response-must-not-be-logged")

    def get(self, service, path, **kwargs):
        if path == "/api/v1/events":
            raise self.failure
        return super().get(service, path, **kwargs)


def collect(db, pods=(), events=(), *, client=None, now=NOW, tenant=11, project=111, cluster=CL):
    ctx = RunContext(client=client or Client(pods, events), account="synthetic", project_id="synthetic",
                     db=db, tenant_id=tenant, project_ref=project)
    current = collector._Run(ctx, now)
    observations = current.cluster(*cluster)
    current.finish()
    db.commit()
    return current, observations


def facts(observations, check="pod_evicted"):
    return {tuple(o.resource_id.split("/")): o.facts for o in observations if o.check_item == check}


def workload(name="alpha", *, ns="ns", kind="StatefulSet", cid=CL[0]):
    return cid, ns, kind, name


@pytest.mark.parametrize("status", [0, 403, 503])
def test_event_fetch_failure_keeps_complete_pod_status_evidence(mem_db, status, caplog):
    initial = [scoped_pod(), scoped_pod("beta-0", "uid-b", workload="beta"),
               scoped_pod("gamma-0", "uid-c", workload="gamma")]
    collect(mem_db, initial)
    at = NOW + timedelta(minutes=1)
    current_pods = [
        scoped_pod(rc=2, ready=False, state={"waiting": {"reason": "CrashLoopBackOff"}}, last=terminated(at=at)),
        scoped_pod("beta-0", "uid-b", workload="beta", ready=False, phase="Failed",
                   state=terminated("OOMKilled", at, 137)),
        scoped_pod("gamma-0", "uid-c", workload="gamma", rc=4, last=terminated("OOMKilled", at, 137)),
    ]
    caplog.set_level(logging.INFO, logger="app.k8s.collector")
    current, observations = collect(mem_db, client=EventFailure(current_pods, status), now=at)
    states = list(mem_db.scalars(select(PodState)))
    assert len(states) == 3 and all(s.last_seen == at and not s.deleted for s in states)
    events = list(mem_db.scalars(select(PodEvent)))
    assert {(e.workload, e.event_type, e.count) for e in events} == {
        ("alpha", "RESTART", 2), ("alpha", "CRASH_LOOP", 2),
        ("beta", "OOM_KILLED", 1), ("gamma", "OOM_KILLED", 1), ("gamma", "RESTART", 3),
    }
    assert all(e.source == "POD_STATUS" for e in events)
    assert facts(observations, "pod_crashloop")[workload()]["crashloop_pods"] == 1
    assert facts(observations, "pod_restart")[workload("gamma")] == {"restarts_10m": 4, "restarts_1h": 4}
    # Aggregate OOM/eviction facts require Event evidence as well; an incomplete
    # source cannot turn a partial count into a recovery value, including nonzero values.
    assert not facts(observations, "pod_oom") and not facts(observations)
    assert len(current.ctx.errors) == 1 and f"HTTP {status}" in current.ctx.errors[0]
    failures = [r for r in caplog.records if getattr(r, "phase", "") == "event_fetch"]
    assert len(failures) == 1 and failures[0].upstream_status == status
    assert "synthetic-response-must-not-be-logged" not in caplog.text


def test_failed_event_source_does_not_expire_ongoing_events_or_publish_recovery(mem_db):
    oom = scoped_event("oom")
    oom["reason"] = "OOMKilling"
    bp = scoped_event("pressure", first=NOW - timedelta(minutes=10))
    bp.update(reason="BackPressure", message="backpressure")
    collect(mem_db, [scoped_pod()], [scoped_event(), oom, bp])
    pressure = mem_db.scalar(select(PodEvent).where(PodEvent.event_type == "BACKPRESSURE"))
    assert pressure.ongoing
    saved = deepcopy(pressure.detail)
    _, observations = collect(mem_db, client=EventFailure([scoped_pod()]), now=NOW + timedelta(hours=2))
    assert pressure.ongoing and pressure.end_time is None and pressure.detail == saved
    assert not facts(observations) and not facts(observations, "pod_oom")
    assert not facts(observations, "pod_backpressure")
    assert facts(observations, "pod_restart")[workload()]["restarts_10m"] == 0


def test_pipeline_event_api_error_retains_alarms_and_still_fires_crashloop(mem_db, collect_task):
    run_task, _ = collect_task
    run_task(events=[event()])
    eviction = mem_db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-CCE-POD-005"))
    assert eviction is not None and eviction.status == "FIRING"
    result = run_task(minute=61, fail_events=True,
                      pods=[pod(ready=False, state={"waiting": {"reason": "CrashLoopBackOff"}})])
    assert result.status == "PARTIAL" and "503" in result.error_summary
    mem_db.refresh(eviction)
    assert eviction.status == "FIRING" and eviction.resolved_at is None
    crash = mem_db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-CCE-POD-003"))
    assert crash is not None and crash.status == "FIRING"
    assert mem_db.scalar(select(PodState)).last_seen == NOW + timedelta(minutes=61)
    assert mem_db.scalar(select(Notification).where(Notification.alarm_id == eviction.id,
                                                    Notification.kind == "RESOLVED")) is None


class IncompleteList(Client):
    def __init__(self, pods, events=(), *, source="pods", mode="transport"):
        super().__init__(pods, events)
        self.source, self.mode = source, mode

    def get(self, service, path, **kwargs):
        if path != f"/api/v1/{self.source}":
            return super().get(service, path, **kwargs)
        self.requests.append((service, path, kwargs))
        if not kwargs["params"].get("continue"):
            items = self.pods if self.source == "pods" else self.events
            return {"items": deepcopy(items[:1]), "metadata": {"resourceVersion": "1", "continue": "opaque"}}
        if self.mode == "transport":
            raise HcsError(503, "TEST.PAGE_FAILED", "synthetic page failure")
        return {"items": [], "metadata": {"resourceVersion": "2"}}


@pytest.mark.parametrize("mode", ["transport", "version_change"])
def test_incomplete_pod_list_cannot_change_snapshots_events_or_resource_lifecycle(mem_db, mode, monkeypatch):
    second = scoped_pod("beta-0", "uid-b", workload="beta", ready=False,
                        state={"waiting": {"reason": "CrashLoopBackOff"}})
    collect(mem_db, [scoped_pod(), second])
    resource = Resource(service="cce", resource_type="cce_workload", resource_id="/".join(workload("beta")),
                        name="beta", tenant_id=11, project_ref=111)
    mem_db.add(resource)
    mem_db.commit()
    before = [(s.key, s.deleted, s.ready, s.last_seen) for s in mem_db.scalars(select(PodState).order_by(PodState.key))]
    client = IncompleteList([scoped_pod()], mode=mode)

    def get(service, path, **kwargs):
        if service == "cce":
            return {"items": [{"metadata": {"uid": CL[0], "name": CL[1]}, "status": {"phase": "Available"}}]}
        return client.get(service, path, **kwargs)

    monkeypatch.setattr(collector, "utcnow", lambda: NOW + timedelta(minutes=1))
    ctx = RunContext(client=type("Cloud", (), {"get": staticmethod(get)})(), account="synthetic",
                     project_id="synthetic", db=mem_db, tenant_id=11, project_ref=111)
    assert collector.PodEventCollector().collect(ctx) == []
    mem_db.commit()
    assert ctx.errors
    assert before == [(s.key, s.deleted, s.ready, s.last_seen)
                      for s in mem_db.scalars(select(PodState).order_by(PodState.key))]
    assert mem_db.scalar(select(PodEvent)).ongoing
    assert resource.lifecycle == "ACTIVE" and resource.deleted_at is None
    assert all(path != "/api/v1/events" for _, path, _ in client.requests)


@pytest.mark.parametrize("mode", ["transport", "version_change"])
def test_incomplete_event_list_is_not_used_as_partial_evidence(mem_db, mode):
    pods = [scoped_pod(ready=False, state={"waiting": {"reason": "CrashLoopBackOff"}})]
    current, observations = collect(mem_db, client=IncompleteList(pods, [scoped_event(count=9)],
                                                                 source="events", mode=mode))
    assert current.ctx.errors and mem_db.scalar(select(PodState)) is not None
    assert mem_db.scalar(select(PodEvent).where(PodEvent.event_type == "EVICTED")) is None
    assert facts(observations, "pod_crashloop")[workload()]["crashloop_pods"] == 1
    assert not facts(observations)


@pytest.mark.parametrize("response", [
    {},
    {"items": None},
    {"items": [{"metadata": {"uid": "event-a"}, "involvedObject": {}, "series": "invalid"}]},
])
def test_malformed_event_source_keeps_pod_status_but_not_event_zeroes(mem_db, response):
    pods = [scoped_pod(ready=False, state={"waiting": {"reason": "CrashLoopBackOff"}})]

    class Malformed(Client):
        def get(self, service, path, **kwargs):
            return deepcopy(response) if path == "/api/v1/events" else super().get(service, path, **kwargs)

    current, observations = collect(mem_db, client=Malformed(pods))
    assert len(current.ctx.errors) == 1 and mem_db.scalar(select(PodState)) is not None
    assert facts(observations, "pod_crashloop")[workload()]["crashloop_pods"] == 1
    assert not facts(observations) and not facts(observations, "pod_oom")
    assert not current.event_diagnostics


def test_complete_empty_pod_list_still_confirms_deletion_when_event_source_fails(mem_db):
    collect(mem_db, [scoped_pod(ready=False, state={"waiting": {"reason": "CrashLoopBackOff"}})])
    current, _ = collect(mem_db, client=EventFailure([]), now=NOW + timedelta(minutes=1))
    assert current.ctx.errors and mem_db.scalar(select(PodState)).deleted
    crash = mem_db.scalar(select(PodEvent))
    assert not crash.ongoing and crash.detail["recovery_confirmed"] is False


@pytest.mark.parametrize("issue,diagnostic", [
    ("invalid", "event_count_invalid"),
    ("counter", "event_counter_regressed"),
    ("time", "event_time_regressed"),
])
def test_invalid_or_regressed_event_only_masks_its_verified_workload_and_type(mem_db, issue, diagnostic, caplog):
    pods = [scoped_pod(), scoped_pod("beta-0", "uid-b", workload="beta"),
            scoped_pod("job-0", "uid-j", workload="alpha", kind="Job")]
    item = scoped_event(count=5, first=NOW - timedelta(minutes=1))
    if issue != "invalid":
        collect(mem_db, pods, [item])
    if issue == "invalid":
        item["count"] = 0
    elif issue == "counter":
        item["count"] = 2
    else:
        item["lastTimestamp"] = (NOW - timedelta(seconds=1)).isoformat()
    caplog.set_level(logging.INFO, logger="app.k8s.collector")
    current, observations = collect(mem_db, pods, [item, scoped_event("event-b", 2, name="beta-0", pod_uid="uid-b")],
                                    now=NOW + timedelta(minutes=1))
    by_workload = facts(observations)
    assert workload() not in by_workload
    assert by_workload[workload("beta")] == {"evicted_1h": 2}
    assert by_workload[workload(kind="Job")] == {"evicted_1h": 0}
    assert facts(observations, "pod_oom")[workload()] == {"oom_1h": 0}
    assert facts(observations, "pod_crashloop")[workload()]["crashloop_pods"] == 0
    assert not current.ctx.errors and current.ctx.stats["event_data_diagnostics"] == {diagnostic: 1}
    diagnostics = [r for r in caplog.records if getattr(r, "phase", "") == "event_validation"]
    assert len(diagnostics) == 1 and diagnostics[0].levelno == logging.INFO
    if issue != "invalid":
        stored = mem_db.scalar(select(PodEvent).where(PodEvent.workload == "alpha"))
        assert stored.count == 5 and stored.last_time == NOW


def test_identical_uid_and_workload_in_different_namespaces_are_independent(mem_db):
    pods = [scoped_pod(), scoped_pod(namespace="other")]
    current, observations = collect(mem_db, pods, [scoped_event(count=0), scoped_event("other", 2, namespace="other")])
    assert workload() not in facts(observations)
    assert facts(observations)[workload(ns="other")] == {"evicted_1h": 2}
    assert not current.ctx.errors


@pytest.mark.parametrize("tenant,project", [(22, 222), (11, 112)])
def test_invalid_event_cannot_contaminate_identical_other_tenant_or_project(mem_db, tenant, project):
    collect(mem_db, [scoped_pod()], [scoped_event(count=5)])
    current, observations = collect(mem_db, [scoped_pod()], [scoped_event(count=0)])
    assert not facts(observations) and not current.ctx.errors
    other, observations = collect(mem_db, [scoped_pod()], [scoped_event(count=2)], tenant=tenant, project=project)
    assert facts(observations)[workload()] == {"evicted_1h": 2} and not other.ctx.errors
    saved = mem_db.scalar(select(PodEvent).where(PodEvent.tenant_id == 11, PodEvent.project_ref == 111))
    assert saved.count == 5
    own, observations = collect(mem_db, [scoped_pod()], [scoped_event(count=6)])
    assert facts(observations)[workload()] == {"evicted_1h": 6} and not own.ctx.errors


class MultiCluster:
    def __init__(self, data):
        self.data = data

    def get(self, service, path, **kwargs):
        if service == "cce":
            return {"items": [{"metadata": {"uid": cid, "name": cid}, "status": {"phase": "Available"}}
                              for cid in self.data]}
        pods, events = self.data[kwargs["host_vars"]["cluster_id"]]
        value = pods if path == "/api/v1/pods" else events
        if isinstance(value, HcsError):
            raise value
        return {"items": deepcopy(value)}


@pytest.mark.parametrize("failure", ["invalid", "api"])
def test_event_gaps_are_bounded_to_one_cluster_in_the_same_run(mem_db, monkeypatch, failure):
    bad = [scoped_event(count=0)] if failure == "invalid" else HcsError(403, "TEST.DENIED", "synthetic")
    client = MultiCluster({CL[0]: ([scoped_pod()], bad), "cluster-b": ([scoped_pod()], [scoped_event(count=2)])})
    monkeypatch.setattr(collector, "utcnow", lambda: NOW)
    ctx = RunContext(client=client, account="synthetic", project_id="synthetic", db=mem_db, tenant_id=11, project_ref=111)
    observations = collector.PodEventCollector().collect(ctx)
    mem_db.commit()
    assert workload() not in facts(observations)
    assert facts(observations)[workload(cid="cluster-b")] == {"evicted_1h": 2}
    assert bool(ctx.errors) is (failure == "api")
    assert len(list(mem_db.scalars(select(PodState)))) == 2


@pytest.mark.parametrize("identity", ["uid_only", "direct_workload"])
def test_verified_uid_or_explicit_workload_can_limit_uncertainty(mem_db, identity):
    item = scoped_event(count=0)
    if identity == "uid_only":
        item["involvedObject"].pop("name")
    else:
        item["involvedObject"].update(kind="StatefulSet", name="alpha", uid="owner-a")
    _, observations = collect(mem_db, [scoped_pod(), scoped_pod("beta-0", "uid-b", workload="beta")], [item])
    assert workload() not in facts(observations)
    assert facts(observations)[workload("beta")] == {"evicted_1h": 0}


@pytest.mark.parametrize("identity", ["no_uid", "old_uid", "wrong_name", "unknown_kind", "no_object", "duplicate_uid"])
def test_unproven_object_falls_back_to_known_namespace_without_name_heuristics(mem_db, identity):
    pods = [scoped_pod(), scoped_pod("beta-0", "uid-b", workload="beta"), scoped_pod(namespace="other")]
    item = scoped_event(count=0)
    if identity == "no_uid":
        item["involvedObject"].pop("uid")
    elif identity == "old_uid":
        item["involvedObject"]["uid"] = "replaced-uid"
    elif identity == "wrong_name":
        item["involvedObject"]["name"] = "beta-0"
    elif identity == "unknown_kind":
        item["involvedObject"]["kind"] = "UnknownObject"
    elif identity == "no_object":
        item["involvedObject"] = {}
    else:
        pods[1]["metadata"]["uid"] = "uid-a"
    _, observations = collect(mem_db, pods, [item])
    assert set(facts(observations)) == {workload(ns="other")}
    assert facts(observations)[workload(ns="other")] == {"evicted_1h": 0}


@pytest.mark.parametrize("identity", ["no_namespace", "node"])
def test_unidentifiable_or_cluster_scoped_object_falls_back_to_cluster(mem_db, identity):
    item = scoped_event(count=0)
    if identity == "no_namespace":
        item["involvedObject"].pop("namespace")
        item["metadata"].pop("namespace")
    else:
        item["involvedObject"].update(kind="Node", name="synthetic-node", uid="node-uid")
    _, observations = collect(mem_db, [scoped_pod(), scoped_pod(namespace="other")], [item])
    assert not facts(observations)
    assert len(facts(observations, "pod_crashloop")) == 2


def test_previous_event_uid_retains_verified_old_owner_after_pod_replacement(mem_db):
    collect(mem_db, [scoped_pod()], [scoped_event(count=5)])
    pods = [scoped_pod("alpha-1", "uid-a2"), scoped_pod(uid="new-uid", workload="beta"),
            scoped_pod("gamma-0", "uid-c", workload="gamma")]
    current, observations = collect(mem_db, pods, [scoped_event(count=0)], now=NOW + timedelta(minutes=1))
    by_workload = facts(observations)
    assert workload() not in by_workload
    assert by_workload[workload("beta")] == by_workload[workload("gamma")] == {"evicted_1h": 0}
    saved = mem_db.scalar(select(PodEvent))
    assert saved.count == 5 and saved.workload == "alpha" and not current.ctx.errors


def test_regressed_update_keeps_previous_scope_unknown_even_when_new_identity_changes(mem_db):
    pods = [scoped_pod(), scoped_pod("beta-0", "uid-b", workload="beta"), scoped_pod(namespace="other")]
    collect(mem_db, pods, [scoped_event(count=5)])
    changed = scoped_event(count=2, name="beta-0", pod_uid="uid-b")
    current, observations = collect(mem_db, pods, [changed], now=NOW + timedelta(hours=2))
    assert set(facts(observations)) == {workload(ns="other")}
    assert not current.ctx.errors
    saved = mem_db.scalar(select(PodEvent))
    assert saved.count == 5 and saved.workload == "alpha"


def test_regressed_event_older_than_retention_still_protects_the_last_verified_scope(mem_db):
    pods = [scoped_pod(), scoped_pod("beta-0", "uid-b", workload="beta")]
    collect(mem_db, pods, [scoped_event(count=5)])
    stale = scoped_event(count=2, first=NOW - timedelta(days=90), last=NOW - timedelta(days=90))
    current, observations = collect(mem_db, pods, [stale], now=NOW + timedelta(hours=2))
    assert workload() not in facts(observations)
    assert facts(observations)[workload("beta")] == {"evicted_1h": 0}
    saved = mem_db.scalar(select(PodEvent))
    assert saved.count == 5 and saved.last_time == NOW
    assert not current.ctx.errors
    assert current.ctx.stats["event_data_diagnostics"] == {"event_counter_regressed": 1}


def test_one_pod_gap_masks_its_whole_workload_but_not_another_workload(mem_db):
    pods = [scoped_pod(), scoped_pod("alpha-1", "uid-a2"), scoped_pod("beta-0", "uid-b", workload="beta")]
    items = [scoped_event(count=0), scoped_event("event-a2", 3, name="alpha-1", pod_uid="uid-a2"),
             scoped_event("event-b", 2, name="beta-0", pod_uid="uid-b")]
    _, observations = collect(mem_db, pods, items)
    assert workload() not in facts(observations)
    assert facts(observations)[workload("beta")] == {"evicted_1h": 2}


def test_backpressure_precision_gap_does_not_hide_another_workload_signal(mem_db):
    pods = [scoped_pod(), scoped_pod("beta-0", "uid-b", workload="beta")]
    invalid = scoped_event(count=0)
    valid = scoped_event("event-b", name="beta-0", pod_uid="uid-b", first=NOW - timedelta(minutes=6))
    for item in (invalid, valid):
        item.update(reason="BackPressure", message="backpressure")
    current, observations = collect(mem_db, pods, [invalid, valid])
    assert set(facts(observations, "pod_backpressure")) == {workload("beta")}
    assert facts(observations, "pod_backpressure")[workload("beta")]["backpressure_min"] == 6
    assert "backpressure_min" not in next(o.metrics for o in observations
                                          if o.check_item == "pod_restart" and o.resource_id.endswith("/alpha"))
    assert not current.ctx.errors


@pytest.mark.parametrize("issue", ["invalid", "counter", "time"])
def test_scoped_diagnostics_preserve_alarm_but_allow_other_workload_alarm(mem_db, collect_task, issue, caplog):
    run_task, _ = collect_task
    pods = [scoped_pod(), scoped_pod("beta-0", "uid-b", workload="beta")]
    run_task(pods=pods, events=[scoped_event(count=5, first=NOW - timedelta(minutes=1))])
    alarm = mem_db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-CCE-POD-005"))
    assert alarm is not None and alarm.status == "FIRING"
    invalid = scoped_event(count=0 if issue == "invalid" else 2 if issue == "counter" else 5,
                           first=NOW - timedelta(minutes=1), last=NOW - timedelta(seconds=1) if issue == "time" else NOW)
    caplog.set_level(logging.INFO, logger="app.k8s.collector")
    for minute in range(61, 65):
        at = NOW + timedelta(minutes=minute)
        result = run_task(pods=pods, events=[invalid, scoped_event("event-b", 2, name="beta-0", pod_uid="uid-b",
                                                                 first=at, last=at)], minute=minute)
        assert result.status == "SUCCESS" and result.error_summary is None
    mem_db.refresh(alarm)
    assert alarm.status == "FIRING" and alarm.resolved_at is None
    eviction_alarms = list(mem_db.scalars(select(AlarmEvent).where(AlarmEvent.rule_id == "R-CCE-POD-005")))
    assert len(eviction_alarms) == 2 and all(a.status == "FIRING" for a in eviction_alarms)
    assert mem_db.scalar(select(Notification).where(Notification.alarm_id == alarm.id,
                                                    Notification.kind == "RESOLVED")) is None
    assert mem_db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-PLAT-001")) is None
    diagnostics = [r for r in caplog.records if getattr(r, "phase", "") == "event_validation"]
    assert len(diagnostics) == 4 and all(r.levelno == logging.INFO for r in diagnostics)
