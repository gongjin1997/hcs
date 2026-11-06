"""CCE PDF / native Kubernetes semantic regressions; synthetic objects, no remote API proof."""
import logging
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from app.collectors.base import RunContext
from app.hcs import HcsError
from app.k8s.collector import _Run
from app.k8s.kube import KubeApi, aggregate, parse_metric
from app.models import PodEvent, PodProbe, PodState
from app.schemas.pod import ProbeIn

from .test_pod_scope_isolation import pod_api as pod_api  # noqa: F401

NOW = datetime(2026, 11, 2, 2, tzinfo=UTC)
CL = ("cluster-a", "synthetic")


def pod(uid="pod-1", rc=0, *, ready=True, phase="Running", state=None, last=None, owner=None):
    return {"metadata": {"uid": uid, "namespace": "ns", "name": "worker-0",
                         "ownerReferences": owner or [{"kind": "StatefulSet", "name": "worker", "controller": True}]},
            "spec": {"containers": [{"name": "main"}]},
            "status": {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                       "containerStatuses": [{"name": "main", "restartCount": rc, "ready": ready,
                                               "state": state or {"running": {}}, "lastState": last or {}}]}}


def terminated(reason="Error", at=NOW, exit_code=1):
    return {"terminated": {"reason": reason, "exitCode": exit_code, "finishedAt": at.isoformat()}}


def event(uid="event-1", count=1, *, first=NOW, last=NOW, kind="Pod", name="worker-0", pod_uid="pod-1"):
    return {"metadata": {"uid": uid, "namespace": "ns"},
            "involvedObject": {"kind": kind, "namespace": "ns", "name": name, "uid": pod_uid},
            "type": "Warning", "reason": "Evicted", "message": "eviction",
            "firstTimestamp": first.isoformat(), "lastTimestamp": last.isoformat(), "count": count}


class Client:
    def __init__(self, pods=(), events=(), pressure="queue_depth 2\n"):
        self.pods, self.events, self.pressure = list(pods), list(events), pressure
        self.requests = []

    def get(self, service, path, **kwargs):
        self.requests.append((service, path, kwargs))
        assert service == "cce_k8s"
        if path == "/api/v1/pods":
            items = self.pods
        elif path == "/api/v1/events":
            items = self.events
        else:
            raise AssertionError(path)
        return {"items": deepcopy(items), "metadata": {"resourceVersion": "1"}}

    def get_text(self, service, path, **kwargs):
        self.requests.append((service, path, kwargs))
        if isinstance(self.pressure, Exception):
            raise self.pressure
        return self.pressure


def run(db, pods=(), events=(), *, now=NOW, client=None):
    context = RunContext(client=client or Client(pods, events), account="synthetic", project_id="project-a",
                         db=db, tenant_id=11, project_ref=111)
    current = _Run(context, now)
    observations = current.cluster(*CL)
    current.finish()
    db.commit()
    return current, observations


def rows(db, kind=None):
    q = select(PodEvent)
    if kind:
        q = q.where(PodEvent.event_type == kind)
    return list(db.scalars(q))


def test_pod_uid_replacement_does_not_compare_restart_counters(mem_db):
    run(mem_db, [pod(rc=8, last=terminated(at=NOW - timedelta(minutes=1)))])
    run(mem_db, [pod("pod-2", rc=9, last=terminated(at=NOW))])
    assert len(rows(mem_db, "RESTART")) == 2
    assert {e.detail["pod_uid"] for e in rows(mem_db)} == {"pod-1", "pod-2"}
    assert all(e.count == 1 for e in rows(mem_db))


def test_uid_scopes_crash_episodes_and_old_event_is_not_recovered(mem_db):
    run(mem_db, [pod(state={"waiting": {"reason": "CrashLoopBackOff"}}, ready=False)])
    run(mem_db, [pod("pod-2", state={"waiting": {"reason": "CrashLoopBackOff"}}, ready=False)])
    events = rows(mem_db, "CRASH_LOOP")
    assert len(events) == 2
    assert events[0].detail["closure_reason"] == "POD_REPLACED"
    assert events[0].detail["recovery_confirmed"] is False
    assert not events[0].ongoing and events[1].ongoing


def test_counter_reset_within_uid_does_not_suppress_later_restart(mem_db):
    run(mem_db, [pod(rc=1, last=terminated(at=NOW))])
    run(mem_db, [pod(rc=0)], now=NOW + timedelta(minutes=1))
    run(mem_db, [pod(rc=1, last=terminated(at=NOW + timedelta(minutes=2)))], now=NOW + timedelta(minutes=2))
    assert len(rows(mem_db, "RESTART")) == 2


def test_last_oom_does_not_attribute_every_restart_in_delta_to_oom(mem_db):
    run(mem_db, [pod()])
    run(mem_db, [pod(rc=4, last=terminated("OOMKilled", NOW + timedelta(minutes=1), 137))],
        now=NOW + timedelta(minutes=1))
    assert sum(e.count for e in rows(mem_db, "OOM_KILLED")) == 1
    assert sum(e.count for e in rows(mem_db, "RESTART")) == 3


def test_current_terminated_oom_is_recorded_without_a_restart_and_not_duplicated_later(mem_db):
    last = terminated("OOMKilled", NOW, 137)
    run(mem_db, [pod(state=last, ready=False, phase="Failed")])
    assert len(rows(mem_db, "OOM_KILLED")) == 1
    run(mem_db, [pod(rc=1, last=last)], now=NOW + timedelta(minutes=1))
    assert len(rows(mem_db, "OOM_KILLED")) == 1


def test_init_container_crashloop_and_restart_are_monitored(mem_db):
    p = pod()
    p["spec"]["initContainers"] = [{"name": "init"}]
    p["status"]["initContainerStatuses"] = [{"name": "init", "restartCount": 2, "ready": False,
        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
        "lastState": terminated(at=NOW - timedelta(seconds=30))}]
    run(mem_db, [p])
    assert any(e.container == "init" for e in rows(mem_db, "CRASH_LOOP"))
    assert any(e.container == "init" for e in rows(mem_db, "RESTART"))


def test_pod_ready_condition_overrides_all_container_ready_flags(mem_db):
    p = pod()
    p["status"]["conditions"] = [{"type": "Ready", "status": "False", "reason": "ReadinessGatesNotReady"}]
    run(mem_db, [p])
    assert mem_db.scalar(select(PodState)).ready is False


def test_running_but_not_ready_does_not_confirm_crashloop_recovery(mem_db):
    run(mem_db, [pod(state={"waiting": {"reason": "CrashLoopBackOff"}}, ready=False)])
    run(mem_db, [pod(ready=False)], now=NOW + timedelta(minutes=1))
    assert rows(mem_db, "CRASH_LOOP")[0].ongoing
    run(mem_db, [pod()], now=NOW + timedelta(minutes=2))
    assert rows(mem_db, "CRASH_LOOP")[0].detail["recovery_confirmed"] is True


def test_deleted_pod_is_not_reported_as_recovered(mem_db):
    run(mem_db, [pod(state={"waiting": {"reason": "CrashLoopBackOff"}}, ready=False)])
    run(mem_db)
    ev = rows(mem_db, "CRASH_LOOP")[0]
    assert ev.detail["closure_reason"] == "POD_DELETED" and ev.detail["recovery_confirmed"] is False


def test_direct_controller_is_preserved_without_name_based_deployment_or_cronjob_guess(mem_db):
    p = pod(owner=[{"kind": "ReplicaSet", "name": "standalone-rs", "controller": True}])
    run(mem_db, [p])
    st = mem_db.scalar(select(PodState))
    assert (st.workload_kind, st.workload) == ("ReplicaSet", "standalone-rs")


def test_noncontroller_owner_reference_is_not_a_workload(mem_db):
    p = pod(owner=[{"kind": "Job", "name": "batch-20261102", "controller": False}])
    run(mem_db, [p])
    st = mem_db.scalar(select(PodState))
    assert (st.workload_kind, st.workload) == ("Pod", "worker-0")


def test_event_for_replaced_uid_does_not_inherit_current_owner(mem_db):
    run(mem_db, [pod("pod-2")], [event(pod_uid="pod-1")])
    ev = rows(mem_db, "EVICTED")[0]
    assert (ev.workload_kind, ev.workload) == ("Pod", "worker-0")


def test_event_without_object_uid_does_not_claim_current_pod_owner(mem_db):
    e = event()
    e["involvedObject"].pop("uid")
    run(mem_db, [pod()], [e])
    ev = rows(mem_db, "EVICTED")[0]
    assert (ev.workload_kind, ev.workload) == ("Pod", "worker-0")
    assert ev.detail["identity_precision"] == "NAME_ONLY"


def test_same_workload_name_in_different_kinds_does_not_share_counts(mem_db):
    p1 = pod()
    p2 = pod("pod-2", owner=[{"kind": "Job", "name": "worker", "controller": True}])
    p2["metadata"]["name"] = "worker-job"
    _, obs = run(mem_db, [p1, p2], [event(count=2)])
    by_kind = {o.resource_id.split("/")[-2]: o.facts for o in obs if o.check_item == "pod_evicted"}
    assert by_kind["StatefulSet"]["evicted_1h"] == 2 and by_kind["Job"]["evicted_1h"] == 0


def test_cumulative_event_count_crossing_window_is_unknown_not_full_count(mem_db):
    old = event(count=100, first=NOW - timedelta(days=2), last=NOW)
    current, obs = run(mem_db, [pod()], [old])
    checks = [o for o in obs if o.check_item == "pod_evicted"]
    assert not checks or "evicted_1h" not in checks[0].facts
    assert not current.ctx.errors
    assert current.ctx.stats["event_window_unknown_checks"] == 1


def test_cumulative_event_window_diagnostic_is_aggregated_across_workloads(mem_db, caplog):
    caplog.set_level(logging.INFO, logger="app.k8s.collector")
    second = pod("pod-2", owner=[{"kind": "StatefulSet", "name": "worker-2", "controller": True}])
    second["metadata"]["name"] = "worker-1"
    events = [event(uid="event-1", count=100, first=NOW - timedelta(days=2)),
              event(uid="event-2", count=100, first=NOW - timedelta(days=2),
                    name="worker-1", pod_uid="pod-2")]
    current, _ = run(mem_db, [pod(), second], events)
    assert not current.ctx.errors
    rows = [record for record in caplog.records if getattr(record, "event", None) == "collection_data_unavailable"]
    assert len(rows) == 1
    assert rows[0].reason == "cumulative_count_crosses_window"
    assert rows[0].levelno == logging.INFO
    assert rows[0].affected_count == rows[0].unknown_event_count == 2


def test_repeated_event_uid_is_monotonic_and_stale_payload_does_not_rewind(mem_db):
    first = event(count=5)
    run(mem_db, [pod()], [first])
    run(mem_db, [pod()], [event(count=3, last=NOW - timedelta(seconds=1))])
    ev = rows(mem_db, "EVICTED")[0]
    assert ev.count == 5 and ev.last_time == NOW


@pytest.mark.parametrize("count", [True, "2", -1, 1.5])
def test_invalid_event_counts_are_evidence_gaps(mem_db, count):
    current, _ = run(mem_db, [pod()], [event(count=count)])
    assert not current.ctx.errors and not rows(mem_db)
    assert current.ctx.stats["event_data_diagnostics"] == {"event_count_invalid": 1}


def test_probe_fetch_failure_breaks_continuous_trigger_window(mem_db):
    probe = PodProbe(tenant_id=11, name="queue", namespace="ns", metric="queue_depth", threshold=1,
                     for_minutes=3, recover_minutes=3)
    mem_db.add(probe)
    mem_db.commit()
    run(mem_db, [pod()], client=Client([pod()]))
    current, _ = run(mem_db, [pod()], now=NOW + timedelta(minutes=2),
                     client=Client([pod()], pressure=HcsError(403, "Forbidden", "denied")))
    assert current.ctx.errors
    run(mem_db, [pod()], now=NOW + timedelta(minutes=4), client=Client([pod()]))
    assert not rows(mem_db, "BACKPRESSURE")


def test_missing_metric_breaks_continuous_recovery_window(mem_db):
    probe = PodProbe(tenant_id=11, name="queue", namespace="ns", metric="queue_depth", threshold=1,
                     for_minutes=1, recover_minutes=3)
    mem_db.add(probe)
    mem_db.commit()
    for minute, text in [(0, "queue_depth 2"), (1, "queue_depth 2"), (2, "queue_depth 0"),
                         (4, "other 0"), (6, "queue_depth 0")]:
        run(mem_db, [pod()], now=NOW + timedelta(minutes=minute), client=Client([pod()], pressure=text))
    assert rows(mem_db, "BACKPRESSURE")[0].ongoing


@pytest.mark.parametrize("path", ["/restart", "/metrics?reset=1", "/%2e%2e/metrics", "//host/metrics"])
def test_probe_configuration_cannot_allow_arbitrary_get_side_effects(path):
    with pytest.raises(ValueError):
        ProbeIn(name="queue", namespace="ns", metric="queue_depth", threshold=1, path=path)


def test_proxy_runtime_rejects_nonmetrics_path_before_request():
    client = Client()
    with pytest.raises(HcsError):
        KubeApi(client, CL[0]).metrics_text("ns", "worker-0", 9000, "/restart")
    assert client.requests == []


def test_event_expiry_is_unknown_and_cannot_emit_a_zero_recovery_observation(mem_db):
    e = event()
    e["reason"], e["message"] = "BackPressure", "backpressure"
    run(mem_db, [pod()], [e])
    current, obs = run(mem_db, [pod()], now=NOW + timedelta(minutes=10))
    ev = rows(mem_db, "BACKPRESSURE")[0]
    assert ev.detail["recovery_confirmed"] is False
    assert not any(o.check_item == "pod_backpressure" for o in obs) and current.ctx.errors


def test_no_probe_or_native_signal_cannot_fabricate_backpressure_zero(mem_db):
    _, obs = run(mem_db, [pod()])
    assert not any(o.check_item == "pod_backpressure" for o in obs)


def test_duplicate_pods_fail_before_updating_snapshot():
    client = Client([pod(), pod()])
    with pytest.raises(HcsError):
        KubeApi(client, CL[0]).pods()


@pytest.mark.parametrize("rc", [True, "2", -1, None])
def test_missing_or_malformed_restart_counter_is_not_zero(rc):
    with pytest.raises(HcsError):
        KubeApi(Client([pod(rc=rc)]), CL[0]).pods()


def test_stats_does_not_count_oom_termination_as_a_restart(pod_api, mem_db):
    client, _user = pod_api
    run(mem_db, [pod(state=terminated("OOMKilled", NOW, 137), ready=False, phase="Failed")])
    result = client.get("/api/v1/pod-events/stats").json()["data"]
    assert result["restarts"] == 0


def test_timeline_marks_truncation_and_counts_restarts_beyond_display_limit(pod_api, mem_db, monkeypatch):
    client, _user = pod_api
    monkeypatch.setattr("app.api.v1.pod_events.utcnow", lambda: NOW)
    for i in range(501):
        mem_db.add(PodEvent(tenant_id=11, project_ref=111, cluster_id=CL[0], cluster_name=CL[1], namespace="ns",
                           pod="worker-0", workload_kind="Pod", workload="worker-0", event_type="RESTART",
                           level="WARNING", source="POD_STATUS", count=1, dedup_key=f"synthetic-{i}",
                           first_time=NOW, last_time=NOW))
    mem_db.commit()
    response = client.get("/api/v1/pod-events/pod", params={"key": "cluster-a/ns/worker-0"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data["events"]) == 500 and data["truncated"] is True and data["total"] == 501
    assert data["restarts_in_window"] == 501


def test_stats_includes_rows_after_the_former_50000_record_cutoff(pod_api, mem_db, monkeypatch):
    client, _user = pod_api
    monkeypatch.setattr("app.api.v1.pod_events.utcnow", lambda: NOW)
    value = {"tenant_id": 11, "project_ref": 111, "cluster_id": CL[0], "cluster_name": CL[1], "namespace": "ns",
             "event_type": "RESTART", "level": "WARNING", "source": "POD_STATUS", "count": 1,
             "dedup_key": "synthetic-capacity", "first_time": NOW, "last_time": NOW}
    mem_db.execute(insert(PodEvent), [value] * 50001)
    mem_db.commit()
    data = client.get("/api/v1/pod-events/stats").json()["data"]
    assert data["total"] == 50001 and data["restarts"] == 50001


def test_timeline_event_anchor_does_not_attach_replacement_pod_snapshot(pod_api, mem_db):
    client, _user = pod_api
    run(mem_db, [pod(rc=1, last=terminated())])
    old = rows(mem_db, "RESTART")[0]
    run(mem_db, [pod("pod-2")])
    result = client.get("/api/v1/pod-events/pod", params={"key": "cluster-a/ns/worker-0", "event_id": old.id})
    assert result.status_code == 200 and result.json()["data"]["state"] is None


def test_unavailable_backpressure_does_not_publish_a_zero_resource_metric(mem_db):
    _, obs = run(mem_db, [pod()])
    assert all("backpressure_min" not in o.metrics for o in obs)


def test_one_successful_probe_target_cannot_clear_a_workload_with_an_unknown_target(mem_db):
    mem_db.add(PodProbe(tenant_id=11, name="queue", namespace="ns", metric="queue_depth", threshold=1))
    mem_db.commit()
    p2 = pod("pod-2")
    p2["metadata"]["name"] = "worker-1"

    class Partial(Client):
        def get_text(self, service, path, **kwargs):
            if "worker-1:" in path:
                raise HcsError(403, "Forbidden", "denied")
            return "queue_depth 0"

    current, obs = run(mem_db, [pod(), p2], client=Partial([pod(), p2]))
    assert current.ctx.errors and not any(o.check_item == "pod_backpressure" for o in obs)


def test_probe_definition_change_restarts_continuous_duration(mem_db):
    probe = PodProbe(tenant_id=11, name="queue", namespace="ns", metric="queue_depth", threshold=1,
                     for_minutes=2, recover_minutes=3)
    mem_db.add(probe)
    mem_db.commit()
    run(mem_db, [pod()], client=Client([pod()]))
    run(mem_db, [pod()], now=NOW + timedelta(minutes=1), client=Client([pod()]))
    probe.threshold = 0.5
    mem_db.commit()
    run(mem_db, [pod()], now=NOW + timedelta(minutes=2), client=Client([pod()]))
    assert not rows(mem_db, "BACKPRESSURE")


def test_eviction_status_and_native_event_are_one_occurrence(mem_db):
    p = pod(ready=False, phase="Failed")
    p["status"]["reason"] = "Evicted"
    _, obs = run(mem_db, [p], [event()])
    assert len(rows(mem_db, "EVICTED")) == 1
    assert next(o.facts["evicted_1h"] for o in obs if o.check_item == "pod_evicted") == 1


def test_invalid_event_count_does_not_publish_zero_for_affected_check(mem_db):
    current, obs = run(mem_db, [pod()], [event(count=True)])
    assert not current.ctx.errors and not any(o.check_item == "pod_evicted" for o in obs)
    assert current.ctx.stats["event_data_diagnostics"] == {"event_count_invalid": 1}


def test_excluded_namespace_is_not_treated_as_deleted(mem_db):
    run(mem_db, [pod()])
    context = RunContext(client=Client([pod()]), account="synthetic", project_id="project-a", db=mem_db,
                         tenant_id=11, project_ref=111, params={"exclude_namespaces": ["ns"]})
    _Run(context, NOW + timedelta(minutes=1)).cluster(*CL)
    mem_db.commit()
    assert mem_db.scalar(select(PodState)).deleted is False


def test_nonfinite_series_cannot_be_dropped_to_make_an_aggregate_look_zero():
    assert parse_metric('queue_depth{shard="1"} NaN\nqueue_depth{shard="2"} 0', "queue_depth") == []
    assert aggregate([float("nan"), 0], "max") is None


@pytest.mark.parametrize("label_filter", ["job", "=worker", "job=a,job=b", "job=a,,task=b"])
def test_malformed_probe_label_filter_is_rejected_instead_of_reading_all_series(label_filter):
    with pytest.raises(ValueError):
        ProbeIn(name="queue", namespace="ns", metric="queue_depth", threshold=1, label_filter=label_filter)


def test_persisted_bad_filter_cannot_silently_match_all_metrics():
    assert parse_metric('queue_depth{job="other"} 0', "queue_depth", "job") == []


def test_a_terminal_container_does_not_confirm_crash_recovery(mem_db):
    run(mem_db, [pod(state={"waiting": {"reason": "CrashLoopBackOff"}}, ready=False)])
    _, obs = run(mem_db, [pod(state=terminated(), ready=False, phase="Failed")], now=NOW + timedelta(minutes=1))
    ev = rows(mem_db, "CRASH_LOOP")[0]
    assert ev.detail["recovery_confirmed"] is False
    assert not any(o.check_item == "pod_crashloop" for o in obs)


@pytest.mark.parametrize("reason,message,kind,event_type,expected", [
    ("Unhealthy", "readiness probe failed", "Pod", "Warning", "PROBE_FAILED"),
    ("FailedScheduling", "insufficient memory", "Pod", "Warning", "SCHEDULE_FAILED"),
    ("ErrImagePull", "image unavailable", "Pod", "Warning", "IMAGE_PULL_FAILED"),
    ("BackOff", "Back-off pulling image", "Pod", "Normal", "IMAGE_PULL_FAILED"),
    ("NodeHasDiskPressure", "disk pressure", "Node", "Warning", "NODE_PRESSURE"),
    ("NodeHasMemoryPressure", "memory pressure", "Node", "Warning", "NODE_PRESSURE"),
    ("ScalingReplicaSet", "scaled replicas", "Deployment", "Normal", "SCALED"),
    ("SuccessfulRescale", "scaled replicas", "HorizontalPodAutoscaler", "Normal", "SCALED"),
    ("CustomWarning", "custom failure", "Pod", "Warning", "WARNING"),
    ("BackPressure", "backpressure", "Pod", "Warning", "BACKPRESSURE"),
    ("SystemOOM", "system OOM", "Node", "Warning", "OOM_KILLED"),
])
def test_each_native_event_family_is_retained_without_fabricating_pod_ownership(
        mem_db, reason, message, kind, event_type, expected):
    e = event(kind=kind, name="worker-0" if kind == "Pod" else "native-object")
    e.update(reason=reason, message=message, type=event_type)
    run(mem_db, [pod()], [e])
    row = rows(mem_db)[0]
    assert row.event_type == expected
    if kind != "Pod":
        assert row.pod == ""
    if kind == "Node":
        assert row.workload == "" and row.node == "native-object"
