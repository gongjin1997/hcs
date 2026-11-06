"""EDOC1100453109 的 CCE 控制接口与 Kubernetes v1.23.17 原生只读契约。"""
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.collectors.base import RunContext
from app.hcs import HcsError
from app.k8s.collector import PodEventCollector, _Run
from app.k8s.kube import KubeApi
from app.ledger.fetchers import fetch_cce
from app.models import PodEvent, Resource

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def control_cluster(phase="Available"):
    return {"kind": "Cluster", "apiVersion": "v3", "metadata": {"uid": "cluster-a", "name": "cce-a"},
            "spec": {"version": "v1.25", "flavor": "cce.s2.small", "type": "VirtualMachine"},
            "status": {"phase": phase, "endpoints": [{"type": "Internal", "url": "https://192.0.2.10:5443"}]}}


def page(items=(), token="", version="91", **metadata):
    return {"kind": "PodList", "apiVersion": "v1", "items": list(items),
            "metadata": {"continue": token, "resourceVersion": version, **metadata}}


class Client:
    def __init__(self, callback):
        self.callback, self.requests = callback, []

    def get(self, service, path, **kwargs):
        self.requests.append((service, path, deepcopy(kwargs)))
        return deepcopy(self.callback(service, path, kwargs))

    def get_text(self, service, path, **kwargs):
        self.requests.append((service, path, deepcopy(kwargs)))
        return "synthetic_queue_depth 7\n"


def ctx(client, db):
    return RunContext(client=client, account="synthetic", project_id="project-a", db=db,
                      tenant_id=11, project_ref=111)


def test_hcs851_cce_control_lists_have_no_invented_offset_limit_or_k8s_continue():
    def fetch(service, path, kwargs):
        assert service == "cce" and not kwargs.get("params")
        if path == "/api/v3/projects/{project_id}/clusters":
            return {"kind": "Cluster", "apiVersion": "v3", "items": [control_cluster()]}
        assert path == "/api/v3/projects/{project_id}/clusters/cluster-a/nodes"
        return {"kind": "List", "apiVersion": "v3", "items": [{
            "kind": "Node", "apiVersion": "v3", "metadata": {"uid": "node-a", "name": "node-a"},
            "status": {"phase": "Active"}, "spec": {"flavor": "synthetic"}}]}
    client = Client(fetch)
    records = fetch_cce(client, {})
    assert len(records) == 1 and records[0].attrs["node_count"] == 1
    assert len(client.requests) == 2


@pytest.mark.parametrize("method,namespace,path", [
    ("pods", None, "/api/v1/pods"), ("events", None, "/api/v1/events"),
    ("pods", "tenant-ns", "/api/v1/namespaces/tenant-ns/pods"),
    ("events", "tenant-ns", "/api/v1/namespaces/tenant-ns/events")])
def test_native_k8s_paths_and_cluster_endpoint_scope(method, namespace, path):
    client = Client(lambda *_: page())
    assert getattr(KubeApi(client, "cluster-a"), method)(namespace) == []
    assert client.requests == [("cce_k8s", path, {"params": {"limit": 500}, "host_vars": {"cluster_id": "cluster-a"}})]


def test_continue_is_opaque_and_short_or_empty_pages_are_not_the_end():
    token = "opaque+/=do-not-parse"
    pages = [page([{"metadata": {"uid": "pod-a", "name": "pod-a", "namespace": "ns"}}], token), page([], "second"),
             page([{"metadata": {"uid": "pod-b", "name": "pod-b", "namespace": "ns"}}])]
    client = Client(lambda *_: pages.pop(0))
    result = KubeApi(client, "cluster-a").pods()
    assert len(result) == 2
    assert [r[2]["params"] for r in client.requests] == [
        {"limit": 500}, {"limit": 500, "continue": token}, {"limit": 500, "continue": "second"}]


@pytest.mark.parametrize("token", [0, False, [], {}])
def test_malformed_falsey_continue_is_not_silently_treated_as_completion(token):
    with pytest.raises(HcsError):
        KubeApi(Client(lambda *_: page(token=token)), "cluster-a").pods()


@pytest.mark.parametrize("body", [{}, {"items": None}, {"items": {}}, {"items": [None]},
                                   {"items": [], "metadata": []},
                                   {"items": [], "metadata": {"resourceVersion": 1}}])
def test_missing_or_malformed_native_list_is_not_empty_healthy(body):
    with pytest.raises(HcsError):
        KubeApi(Client(lambda *_: body), "cluster-a").pods()


def test_changed_resource_version_does_not_mix_two_snapshots():
    pages = [page([{"metadata": {"uid": "one"}}], "next", "91"), page(version="92")]
    with pytest.raises(HcsError) as error:
        KubeApi(Client(lambda *_: pages.pop(0)), "cluster-a").pods()
    assert error.value.code == "LOCAL.INCOMPLETE_PAGE"


@pytest.mark.parametrize("status,code", [(403, "Forbidden"), (404, "NotFound"), (410, "ResourceExpired")])
def test_native_list_permission_service_and_expiry_errors_propagate_without_partial_rows(status, code):
    calls = []
    def fetch(*_):
        calls.append(1)
        if len(calls) == 1:
            return page([{"metadata": {"uid": "one"}}], "next")
        raise HcsError(status, code, "synthetic failure")
    with pytest.raises(HcsError) as error:
        KubeApi(Client(fetch), "cluster-a").pods()
    assert error.value.code == code and len(calls) == 2


def test_repeated_continue_and_page_budget_fail_closed():
    with pytest.raises(HcsError):
        KubeApi(Client(lambda *_: page(token="same")), "cluster-a").pods()
    with pytest.raises(HcsError):
        KubeApi(Client(lambda *_: page(token="more")), "cluster-a")._list("/api/v1/pods", max_pages=1)


def test_pod_metrics_uses_get_proxy_subresource_not_exec_or_arbitrary_service():
    client = Client(lambda *_: {})
    assert KubeApi(client, "cluster-a").metrics_text("tenant-ns", "worker-a", 9100, "/custom/metrics").endswith("7\n")
    assert client.requests == [("cce_k8s", "/api/v1/namespaces/tenant-ns/pods/worker-a:9100/proxy/custom/metrics",
                                {"host_vars": {"cluster_id": "cluster-a"}})]


@pytest.mark.parametrize("path", ["metrics", "//host/metrics", "/../pods", "/x/../../pods", "/./metrics",
                                   "/%2e%2e/pods", "/metrics?query=1", "/metrics#fragment", "/a\\../metrics",
                                   "/a\n/metrics", "/a\x00/metrics"])
def test_metrics_path_cannot_escape_proxy(path):
    client = Client(lambda *_: {})
    with pytest.raises(HcsError):
        KubeApi(client, "cluster-a").metrics_text("ns", "pod-a", 9100, path)
    assert not client.requests


@pytest.mark.parametrize("port", [0, 65536, True, "9100"])
def test_invalid_proxy_port_fails_without_request(port):
    client = Client(lambda *_: {})
    with pytest.raises(HcsError):
        KubeApi(client, "cluster-a").metrics_text("ns", "pod-a", port)
    assert not client.requests


@pytest.mark.parametrize("phase", [None, "", "Unavailable", "Hibernation", "Creating", "Error"])
def test_unavailable_or_unknown_cluster_is_a_reported_collection_gap(phase, mem_db):
    value = control_cluster(phase)
    if phase is None:
        value.pop("status")
    client = Client(lambda *_: {"items": [value]})
    context = ctx(client, mem_db)
    assert PodEventCollector().collect(context) == []
    assert context.errors and len(client.requests) == 1


def test_incomplete_pod_list_preserves_existing_workload(mem_db):
    old = Resource(tenant_id=11, project_ref=111, service="cce", resource_type="cce_workload",
                   resource_id="cluster-a/ns/Deployment/worker", name="worker", lifecycle="ACTIVE")
    mem_db.add(old)
    mem_db.flush()
    def fetch(service, path, kw):
        if service == "cce":
            return {"items": [control_cluster()]}
        if kw["params"].get("continue"):
            raise HcsError(410, "ResourceExpired", "synthetic expired")
        return page([], "next")
    context = ctx(Client(fetch), mem_db)
    assert PodEventCollector().collect(context) == []
    assert context.errors and old.lifecycle == "ACTIVE" and old.deleted_at is None
    assert list(mem_db.scalars(select(PodEvent))) == []


@pytest.mark.parametrize("pod", [{}, {"metadata": {}}, {"metadata": {"name": "pod-a"}},
                                 {"metadata": {"name": None, "namespace": "ns"}}])
def test_nameless_pod_is_a_broken_snapshot_not_a_deleted_workload(pod):
    with pytest.raises(HcsError):
        KubeApi(Client(lambda *_: page([pod])), "cluster-a").pods()


@pytest.mark.parametrize("event", [{}, {"metadata": {"uid": "a"}},
                                   {"metadata": {}, "involvedObject": {}},
                                   {"metadata": {"uid": "a"}, "involvedObject": {}, "series": []}])
def test_malformed_core_event_is_reported_as_a_failed_list(event):
    with pytest.raises(HcsError):
        KubeApi(Client(lambda *_: page([event])), "cluster-a").events()


def test_unverified_control_plane_pagination_is_not_silently_truncated(mem_db):
    client = Client(lambda *_: {"items": [control_cluster()], "metadata": {"continue": "next"}})
    with pytest.raises(HcsError):
        PodEventCollector().collect(ctx(client, mem_db))
    assert len(client.requests) == 1


def test_core_event_series_uses_last_observed_time_and_series_count(mem_db):
    context = ctx(Client(lambda *_: {}), mem_db)
    run = _Run(context, NOW)
    last = NOW - timedelta(seconds=5)
    event = {"apiVersion": "v1", "kind": "Event", "metadata": {"uid": "event-a", "namespace": "ns"},
             "involvedObject": {"kind": "Pod", "name": "worker-abc-xyz", "namespace": "ns"},
             "type": "Warning", "reason": "BackPressure", "message": "backpressure detected",
             "firstTimestamp": None, "lastTimestamp": None, "eventTime": (NOW - timedelta(days=2)).isoformat(),
             "count": 1, "series": {"count": 7, "lastObservedTime": last.isoformat()}}
    run._k8s_events(("cluster-a", "cce-a"), [event], {})
    mem_db.flush()
    row = mem_db.scalar(select(PodEvent))
    assert row is not None and row.last_time == last and row.count == 7
    assert row.first_time == NOW - timedelta(days=2) and row.ongoing
    assert row.tenant_id == 11 and row.project_ref == 111 and row.cluster_id == "cluster-a"


def test_event_without_any_timestamp_is_not_fabricated_at_collection_time(mem_db):
    context = ctx(Client(lambda *_: {}), mem_db)
    run = _Run(context, NOW)
    event = {"metadata": {"uid": "event-a", "namespace": "ns"},
             "involvedObject": {"kind": "Pod", "name": "pod-a", "namespace": "ns"},
             "type": "Warning", "reason": "BackPressure", "message": "backpressure detected"}
    run._k8s_events(("cluster-a", "cce-a"), [event], {})
    mem_db.flush()
    assert not context.errors and list(mem_db.scalars(select(PodEvent))) == []
    assert run.event_diagnostics == {"event_time_missing": 1}
