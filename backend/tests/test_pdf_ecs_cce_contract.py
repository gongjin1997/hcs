"""Uploaded PDF contracts; all IDs and responses are synthetic, with no cloud access.

ECS section 5.2.5: physical PDF pages 86-96 (offset/count/servers).
CCE section 4.2.3: physical pages 148-153 (control plane, no continuation).
CCE chapter 5: physical pages 1280-1281 (native Kubernetes access).
The PDF delegates native Kubernetes pagination; it does not define its fields.
"""
from copy import deepcopy

import pytest

from app.collectors.base import RunContext
from app.collectors.ecs import EcsStatusCollector
from app.hcs import HcsError
from app.k8s import collector as pod_collector
from app.k8s.kube import KubeApi

ECS_PATH = "/v1/{project_id}/cloudservers/detail"
CCE_PATH = "/api/v3/projects/{project_id}/clusters"


class Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, service, path, **kwargs):
        self.requests.append((service, path, deepcopy(kwargs)))
        assert self.responses, "Unexpected additional cloud query"
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


def context(client, **kwargs):
    return RunContext(client=client, account="synthetic", project_id="project-a", **kwargs)


def server(sid="ecs-a", status="ACTIVE", **fields):
    return {"id": sid, "name": sid, "status": status, **fields}


@pytest.fixture
def native_ecs(monkeypatch):
    calls = []

    def metrics(ctx, ids, ip_index):
        calls.append((ids, ip_index))
        return {}, "NONE"

    monkeypatch.setattr(EcsStatusCollector, "_metrics", staticmethod(metrics))
    return calls


@pytest.mark.parametrize("body", [
    {"servers": []}, {"servers": [server()]}, {"count": None, "servers": []},
    {"count": False, "servers": []}, {"count": True, "servers": [server()]},
    {"count": "1", "servers": [server()]}, {"count": 1.0, "servers": [server()]},
    {"count": -1, "servers": []}, {"count": [], "servers": []},
])
def test_ecs_pdf_integer_count_is_required_before_using_a_snapshot(body, native_ecs):
    client = Client([body])
    with pytest.raises(HcsError) as error:
        EcsStatusCollector().collect(context(client))
    assert error.value.code == "LOCAL.INVALID_RESPONSE"
    assert native_ecs == []
    assert client.requests == [("ecs", ECS_PATH, {"params": {"offset": 1, "limit": 100}})]


def test_ecs_short_pages_follow_count_using_page_numbers(native_ecs):
    client = Client([
        {"count": 3, "servers": [server("one")]},
        {"count": 3, "servers": [server("two")]},
        {"count": 3, "servers": [server("three")]},
    ])
    rows = EcsStatusCollector().collect(context(client))
    assert [r.resource_id for r in rows if r.check_item == "instance_status"] == ["one", "two", "three"]
    assert client.requests == [("ecs", ECS_PATH, {"params": {"offset": offset, "limit": 100}})
                               for offset in (1, 2, 3)]
    assert native_ecs == [(["one", "two", "three"], {})]


def test_ecs_exact_page_boundary_does_not_issue_an_extra_request(native_ecs):
    client = Client([{"count": 100, "servers": [server(str(i)) for i in range(100)]}])
    rows = EcsStatusCollector().collect(context(client))
    assert len([r for r in rows if r.check_item == "instance_status"]) == 100
    assert len(client.requests) == 1


def test_ecs_zero_count_and_empty_servers_is_a_valid_empty_list(native_ecs):
    client = Client([{"count": 0, "servers": []}])
    assert EcsStatusCollector().collect(context(client)) == []
    assert len(client.requests) == 1


@pytest.mark.parametrize("last_page", [
    {"count": 2, "servers": []}, {"count": 2}, {"count": 2, "servers": None},
    {"count": 2, "servers": [None]}, {"count": 2, "servers": [server("one")]},
    HcsError(403, "Forbidden", "synthetic denial"),
])
def test_ecs_incomplete_pages_never_reach_metrics_or_return_partial_rows(last_page, native_ecs):
    client = Client([{"count": 2, "servers": [server("one")]}, last_page])
    with pytest.raises(HcsError):
        EcsStatusCollector().collect(context(client))
    assert native_ecs == []


def test_ecs_bad_count_on_later_page_never_returns_the_first_page(native_ecs):
    client = Client([{"count": 2, "servers": [server("one")]}, {"servers": [server("two")]}])
    with pytest.raises(HcsError) as error:
        EcsStatusCollector().collect(context(client))
    assert error.value.code == "LOCAL.INVALID_RESPONSE"
    assert native_ecs == []


@pytest.mark.parametrize("status", [
    "ACTIVE", "BUILD", "ERROR", "HARD_REBOOT", "MIGRATING", "REBOOT", "RESIZE", "REVERT_RESIZE",
    "SHELVED", "SHELVED_OFFLOADED", "SHUTOFF", "UNKNOWN", "VERIFY_RESIZE",
])
def test_ecs_preserves_pdf_status_values_and_nullable_task_state(status, native_ecs):
    client = Client([{"count": 1, "servers": [server(status=status, **{
        "OS-EXT-STS:task_state": None, "OS-EXT-STS:power_state": 0,
    })]}])
    rows = EcsStatusCollector().collect(context(client))
    row = next(r for r in rows if r.check_item == "instance_status")
    assert row.status_raw == status  # 原始状态留证；UNKNOWN 不作为健康/恢复依据。
    assert row.facts["status"] == (None if status == "UNKNOWN" else status)
    assert row.attrs["task_state"] is None and row.facts["task_state"] == ""
    assert row.facts["power_state"] == "NOSTATE"


def test_ecs_deleted_rows_still_count_for_pagination_but_are_not_inspected(native_ecs):
    client = Client([{"count": 2, "servers": [server("gone", "DELETED")]},
                     {"count": 2, "servers": [server("live")]}])
    rows = EcsStatusCollector().collect(context(client))
    assert {r.resource_id for r in rows} == {"live"}
    assert native_ecs == [(["live"], {})]


@pytest.fixture
def pod_run(monkeypatch):
    class Run:
        def __init__(self, ctx, now):
            self.clusters, self.finished = [], False

        def cluster(self, cid, name):
            self.clusters.append((cid, name))
            return []

        def finish(self):
            self.finished = True

    runs = []

    def create(ctx, now):
        run = Run(ctx, now)
        runs.append(run)
        return run

    monkeypatch.setattr(pod_collector, "_Run", create)
    return runs


@pytest.mark.parametrize("continuation", ["next", 0, False, [], {}])
def test_cce_management_never_accepts_nonempty_or_malformed_native_continuation(continuation, pod_run):
    client = Client([{"items": [], "metadata": {"continue": continuation}}])
    with pytest.raises(HcsError) as error:
        pod_collector.PodEventCollector().collect(context(client, db=object()))
    assert error.value.code == "LOCAL.INCOMPLETE_PAGE"
    assert client.requests == [("cce", CCE_PATH, {})]
    assert all(not run.finished and not run.clusters for run in pod_run)


@pytest.mark.parametrize("metadata", [None, {}, {"continue": None}, {"continue": ""}])
def test_cce_management_complete_items_need_no_paging_query(metadata, pod_run):
    client = Client([{"kind": "Cluster", "apiVersion": "v3", "items": [], "metadata": metadata}])
    assert pod_collector.PodEventCollector().collect(context(client, db=object())) == []
    assert client.requests == [("cce", CCE_PATH, {})]
    assert pod_run[0].finished


@pytest.mark.parametrize("body", [{}, {"items": None}, {"items": {}}, {"items": [None]}])
def test_cce_missing_or_malformed_items_does_not_finish_collection(body, pod_run):
    client = Client([body])
    with pytest.raises(HcsError):
        pod_collector.PodEventCollector().collect(context(client, db=object()))
    assert all(not run.finished for run in pod_run)


@pytest.mark.parametrize("phase", ["Available", "Unavailable", "Creating", "Hibernation", "Error"])
def test_cce_management_phase_controls_native_collection(phase, pod_run):
    client = Client([{"items": [{"metadata": {"uid": "cluster-a", "name": "synthetic"},
                                "status": {"phase": phase}}]}])
    ctx = context(client, db=object())
    assert pod_collector.PodEventCollector().collect(ctx) == []
    assert pod_run[0].clusters == ([("cluster-a", "synthetic")] if phase == "Available" else [])
    assert bool(ctx.errors) == (phase != "Available")


@pytest.mark.parametrize("method,namespace,path", [
    ("pods", None, "/api/v1/pods"), ("events", None, "/api/v1/events"),
    ("pods", "ns-a", "/api/v1/namespaces/ns-a/pods"),
    ("events", "ns-a", "/api/v1/namespaces/ns-a/events"),
])
def test_pdf_native_paths_use_cluster_gateway_service(method, namespace, path):
    client = Client([{"items": [], "metadata": {"continue": ""}}])
    assert getattr(KubeApi(client, "cluster-a"), method)(namespace) == []
    assert client.requests == [("cce_k8s", path, {"params": {"limit": 500},
                                               "host_vars": {"cluster_id": "cluster-a"}})]


def test_native_continuation_remains_opaque_and_is_independent_from_management_api():
    token = "opaque+/=token"
    pod = {"metadata": {"uid": "pod-a", "name": "pod-a", "namespace": "ns-a"}}
    client = Client([{"items": [], "metadata": {"continue": token, "resourceVersion": "91"}},
                     {"items": [pod], "metadata": {"continue": "", "resourceVersion": "91"}}])
    assert KubeApi(client, "cluster-a").pods() == [pod]
    assert client.requests == [
        ("cce_k8s", "/api/v1/pods", {"params": {"limit": 500}, "host_vars": {"cluster_id": "cluster-a"}}),
        ("cce_k8s", "/api/v1/pods", {"params": {"limit": 500, "continue": token},
                                    "host_vars": {"cluster_id": "cluster-a"}}),
    ]
