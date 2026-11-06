"""Synthetic native CCE enrichment: no credentials, network, database or AOM."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.hcs import HcsError
from app.k8s.kube import KubeApi
from app.k8s.native_capabilities import (
    collect_resource_metrics,
    discover_resource_metrics,
    parse_quantity,
    resolve_pod_owners,
)

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)
PREFIX = "/apis/metrics.k8s.io"


class Client:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def get(self, service, path, **kwargs):
        self.requests.append((service, path, deepcopy(kwargs)))
        assert service == "cce_k8s"
        assert kwargs["host_vars"] == {"cluster_id": "synthetic-cluster"}
        assert path in self.responses, f"Unexpected synthetic path: {path}"
        response = self.responses[path]
        if callable(response):
            response = response(kwargs)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


def kube(responses=None):
    return KubeApi(Client(responses or {}), "synthetic-cluster")


def page(items=(), *, token="", version="71"):
    return {"metadata": {"resourceVersion": version, "continue": token}, "items": list(items)}


def owner(kind="ReplicaSet", name="deployment-random", uid="rs-uid", **fields):
    return {"apiVersion": f"{'batch' if kind in ('Job', 'CronJob') else 'apps'}/v1",
            "kind": kind, "name": name, "uid": uid, "controller": True, **fields}


def obj(kind="Pod", name="worker-unrelated", uid="pod-uid", namespace="ns", references=None):
    result = {"kind": kind, "apiVersion": {"Pod": "v1", "ReplicaSet": "apps/v1", "Job": "batch/v1"}[kind],
              "metadata": {"namespace": namespace, "name": name, "uid": uid}}
    if references is not None:
        result["metadata"]["ownerReferences"] = references
    return result


def group(*versions, preferred=None):
    versions = versions or ("v1beta1",)
    return {"kind": "APIGroup", "apiVersion": "v1", "name": "metrics.k8s.io",
            "versions": [{"version": v, "groupVersion": f"metrics.k8s.io/{v}"} for v in versions],
            "preferredVersion": {"version": preferred or versions[0],
                                 "groupVersion": f"metrics.k8s.io/{preferred or versions[0]}"}}


def resources(version="v1beta1", names=("pods", "nodes")):
    return {"kind": "APIResourceList", "apiVersion": "v1", "groupVersion": f"metrics.k8s.io/{version}",
            "resources": [{"name": name, "namespaced": name == "pods", "verbs": ["get", "list"],
                           "kind": "PodMetrics" if name == "pods" else "NodeMetrics"} for name in names]}


def sample(kind="pods", name="worker", namespace="ns", cpu="125m", memory="1.5Gi", **fields):
    metadata = {"name": name, **({"namespace": namespace} if kind == "pods" else {})}
    result = {"kind": "PodMetrics" if kind == "pods" else "NodeMetrics", "apiVersion": "metrics.k8s.io/v1beta1",
              "metadata": metadata, "timestamp": (NOW - timedelta(seconds=15)).isoformat(), "window": "30s"}
    if kind == "pods":
        result["containers"] = [{"name": "worker", "usage": {"cpu": cpu, "memory": memory}}]
    else:
        result["usage"] = {"cpu": cpu, "memory": memory}
    return {**result, **fields}


def metrics_kube(pods=None, nodes=None, *, version="v1beta1", namespace=None):
    scoped = f"namespaces/{namespace}/" if namespace else ""
    return kube({PREFIX: group(version), f"{PREFIX}/{version}": resources(version),
                 f"{PREFIX}/{version}/{scoped}pods": page([sample()] if pods is None else pods),
                 f"{PREFIX}/{version}/nodes": page([sample("nodes")] if nodes is None else nodes)})


@pytest.mark.parametrize("enabled", [False, None, 0, 1, "true", [], {}])
def test_opt_in_requires_literal_true_and_never_queries_cloud(enabled):
    api = kube()
    pod = obj(references=[owner()])
    assert resolve_pod_owners(api, [pod], enabled=enabled)["status"] == "disabled"
    assert discover_resource_metrics(api, enabled=enabled)["status"] == "disabled"
    assert collect_resource_metrics(api, enabled=enabled)["status"] == "disabled"
    assert not api.client.requests


@pytest.mark.parametrize("intermediate,top,path", [
    ("ReplicaSet", "Deployment", "/apis/apps/v1/replicasets"),
    ("Job", "CronJob", "/apis/batch/v1/jobs"),
])
def test_owner_chain_verifies_explicit_uid_namespace_kind_and_unrelated_names(intermediate, top, path):
    direct = owner(intermediate, "opaque-middle-name", "middle-uid")
    parent = owner(top, "completely-different-parent", "top-uid")
    pod = obj(references=[direct])
    old = deepcopy(pod)
    api = kube({path: page([obj(intermediate, direct["name"], direct["uid"], references=[parent])])})
    result = resolve_pod_owners(api, [pod], enabled=True)
    row = result["items"][0]
    assert result["status"] == row["status"] == "ok"
    assert row["direct_owner"]["uid"] == "middle-uid"
    assert row["top_owner"] == {"api_version": f"{'apps' if top == 'Deployment' else 'batch'}/v1", "kind": top,
                                "namespace": "ns", "name": "completely-different-parent", "uid": "top-uid"}
    assert row["reason"] == "owner_chain_verified" and pod == old
    assert len(api.client.requests) == 1
    assert api.client.requests[0][2]["params"] == {"limit": 500}


def test_owners_no_guessing_from_names_noncontrolling_reference_or_empty_references():
    api = kube()
    pods = [obj(name="app-123456789-abcde"), obj(references=[owner(controller=False)]),
            obj(references=[owner("StatefulSet", "stable", "stable-uid")])]
    result = resolve_pod_owners(api, pods, enabled=True)
    assert result["status"] == "ok" and not api.client.requests
    assert [r["top_owner"]["kind"] for r in result["items"]] == ["Pod", "Pod", "StatefulSet"]
    assert result["items"][1]["direct_owner"] is None


@pytest.mark.parametrize("references,reason", [
    ({}, "owner_references_invalid"), ([None], "owner_references_invalid"),
    ([owner(controller="true")], "owner_references_invalid"),
    ([owner(), owner(name="other", uid="other-uid")], "multiple_controllers"),
    ([owner(uid="")], "owner_reference_invalid"), ([owner(name="../unsafe")], "owner_reference_invalid"),
    ([owner(namespace="foreign")], "owner_reference_invalid"),
    ([owner(apiVersion="example.org/v1")], "owner_api_group_invalid"),
    ([owner(apiVersion="../../v1")], "owner_reference_invalid"),
])
def test_invalid_controlling_references_are_unknown_without_fetch_or_name_guess(references, reason):
    api = kube()
    result = resolve_pod_owners(api, [obj(references=references)], enabled=True)
    row = result["items"][0]
    assert result["status"] == "partial" and row["status"] == "malformed" and row["reason"] == reason
    assert row["top_owner"] is None and not api.client.requests


@pytest.mark.parametrize("middle,reason", [
    (obj("ReplicaSet", "deployment-random", "new-same-name-uid"), "owner_uid_mismatch"),
    (obj("ReplicaSet", "deployment-random", "rs-uid", namespace="other"), "owner_not_found"),
    (obj("Job", "deployment-random", "rs-uid"), "owner_not_found"),
])
def test_owner_same_name_wrong_uid_namespace_or_kind_cannot_match(middle, reason):
    api = kube({"/apis/apps/v1/replicasets": page([middle])})
    row = resolve_pod_owners(api, [obj(references=[owner()])], enabled=True)["items"][0]
    assert row["reason"] == reason and row["top_owner"] is None
    assert row["direct_owner"]["uid"] == "rs-uid"


def test_owners_duplicate_intermediate_is_ambiguous_not_first_match():
    middle = obj("ReplicaSet", "deployment-random", "rs-uid")
    api = kube({"/apis/apps/v1/replicasets": page([middle, middle])})
    row = resolve_pod_owners(api, [obj(references=[owner()])], enabled=True)["items"][0]
    assert row["reason"] == "owner_ambiguous" and row["top_owner"] is None


@pytest.mark.parametrize("parent,reason", [
    (owner("Deployment", uid="rs-uid"), "owner_cycle"),
    (owner("Deployment", uid="pod-uid"), "owner_cycle"),
    (owner("CronJob", uid="unrelated-uid"), "owner_chain_unsupported"),
    (owner("Deployment", namespace="other"), "owner_reference_invalid"),
    (owner("Deployment", uid=None), "owner_reference_invalid"),
])
def test_owner_chain_rejects_cycles_unsupported_parent_and_invalid_parent(parent, reason):
    middle = obj("ReplicaSet", "deployment-random", "rs-uid", references=[parent])
    api = kube({"/apis/apps/v1/replicasets": page([middle])})
    row = resolve_pod_owners(api, [obj(references=[owner()])], enabled=True)["items"][0]
    assert row["reason"] == reason and row["top_owner"] is None


def test_valid_standalone_intermediate_remains_its_own_top():
    api = kube({"/apis/apps/v1/replicasets": page([obj("ReplicaSet", "deployment-random", "rs-uid")])})
    row = resolve_pod_owners(api, [obj(references=[owner()])], enabled=True)["items"][0]
    assert row["reason"] == "owner_standalone" and row["top_owner"] == row["direct_owner"]


@pytest.mark.parametrize("failure,status,reason", [
    (HcsError(403, "Forbidden", "private payload"), "forbidden", "rbac_forbidden"),
    (HcsError(404, "NotFound", "private payload"), "unsupported", "api_unsupported"),
    (HcsError(503, "Unavailable", "private payload"), "error", "request_failed"),
    (HcsError(401, "Unauthorized", "private payload"), "error", "authentication_failed"),
    (HcsError(0, "LOCAL.INCOMPLETE_PAGE", "private payload"), "partial", "incomplete_list"),
    (HcsError(200, "LOCAL.INVALID_RESPONSE", "private payload"), "malformed", "invalid_response"),
    (RuntimeError("private payload"), "error", "request_failed"),
])
def test_optional_owner_failure_preserves_pods_and_other_kind(failure, status, reason):
    job = obj("Job", "job", "job-uid", references=[owner("CronJob", "schedule", "schedule-uid")])
    api = kube({"/apis/apps/v1/replicasets": failure, "/apis/batch/v1/jobs": page([job])})
    pods = [obj(references=[owner()]), obj(name="job-pod", uid="other-pod", references=[owner("Job", "job", "job-uid")])]
    result = resolve_pod_owners(api, pods, enabled=True)
    failed, good = result["items"]
    assert result["status"] == "partial" and len(result["items"]) == len(pods)
    assert failed["status"] == status and failed["reason"] == reason
    assert failed["direct_owner"]["uid"] == "rs-uid" and failed["top_owner"] is None
    assert good["top_owner"]["kind"] == "CronJob" and good["status"] == "ok"
    assert "private payload" not in repr(result)


def test_no_cache_crosses_kube_instances_and_many_pods_share_one_list():
    pods = [obj(name=f"worker-{i}", uid=f"pod-{i}", references=[owner()]) for i in range(20)]
    middle = obj("ReplicaSet", "deployment-random", "rs-uid", references=[owner("Deployment", "parent", "parent-uid")])
    first = kube({"/apis/apps/v1/replicasets": page([middle])})
    second = kube({"/apis/apps/v1/replicasets": HcsError(403, "Forbidden", "synthetic")})
    assert resolve_pod_owners(first, pods, enabled=True)["status"] == "ok"
    assert len(first.client.requests) == 1
    assert all(r["top_owner"] is None for r in resolve_pod_owners(second, pods, enabled=True)["items"])
    assert len(second.client.requests) == 1


@pytest.mark.parametrize("value,expected", [
    ("0", "0"), ("0n", "0"), ("+0", "0"), ("125m", "0.125"), ("487558164n", "0.487558164"),
    ("42u", "0.000042"), (".5", "0.5"), ("1.", "1"), ("+2", "2"), ("1e-3", "0.001"),
    ("2E+3", "2000"), ("1k", "1000"), ("1M", "1000000"), ("1G", "1000000000"),
    ("1T", "1000000000000"), ("1P", "1000000000000000"), ("1E", "1000000000000000000"),
    ("1Ki", "1024"), ("1Mi", "1048576"), ("1.5Gi", "1610612736"),
    ("1Ti", str(2 ** 40)), ("1Pi", str(2 ** 50)), ("1Ei", str(2 ** 60)),
    ("400m", "0.4"), ("9223372036854775807", "9223372036854775807"),
    ("1.000000001", "1.000000001"), ("0.0000000000", "0"),
])
def test_quantity_exact_nonnegative_normalization(value, expected):
    assert parse_quantity(value) == Decimal(expected)


@pytest.mark.parametrize("value", [
    None, 0, 1, True, False, [], {}, Decimal(1), 0.5, float("inf"), "", " ", " 1", "1 ", "-1", "-1m",
    "NaN", "Infinity", "nan", "inf", "1K", "1ki", "1kiB", "1MB", "1GiB", "1µ", "1e", "1e+", "1e99999999",
    "1e-99999999", "1e64", "0.1n", "1e-10", "8Ei", "9223372036854775808", "1..2", ".", "1\n", "١m", "1" * 129,
])
def test_invalid_quantity_is_unknown_never_zero_or_infinite(value):
    assert parse_quantity(value) is None


@pytest.mark.parametrize("versions,preferred,expected", [
    (("v1beta1",), "v1beta1", "v1beta1"), (("v1beta1", "v1"), "v1", "v1"),
    (("v1beta1", "v1"), "v1beta1", "v1beta1"), (("v9", "v1beta1"), "v9", "v1beta1"),
])
def test_metrics_selects_only_advertised_implemented_versions_and_respects_preference(versions, preferred, expected):
    api = kube({PREFIX: group(*versions, preferred=preferred), f"{PREFIX}/{expected}": resources(expected)})
    result = discover_resource_metrics(api, enabled=True)
    assert result["status"] == "ok" and result["version"] == expected
    assert [r[1] for r in api.client.requests] == [PREFIX, f"{PREFIX}/{expected}"]


@pytest.mark.parametrize("response,reason", [
    ({}, "group_discovery_invalid"), ({"kind": "Status", "code": 200}, "group_discovery_invalid"),
    ({**group(), "versions": []}, "group_discovery_invalid"),
    ({**group(), "name": "other.k8s.io"}, "group_discovery_invalid"),
    ({**group(), "versions": [None]}, "group_discovery_invalid"),
    ({**group(), "versions": [{"version": "v1beta1", "groupVersion": "../v1beta1"}]}, "group_discovery_invalid"),
    ({**group(), "versions": group()["versions"] * 2}, "group_discovery_invalid"),
    ({**group(), "preferredVersion": "v1beta1"}, "preferred_version_invalid"),
    ({**group(), "preferredVersion": {"version": "../../api", "groupVersion": "metrics.k8s.io/../../api"}},
     "preferred_version_invalid"),
])
def test_discovery_malformed_or_injected_path_does_not_request_guessed_endpoint(response, reason):
    api = kube({PREFIX: response})
    result = discover_resource_metrics(api, enabled=True)
    assert result["status"] == "malformed" and result["reason"] == reason
    assert len(api.client.requests) == 1


def test_unknown_advertised_schema_is_unsupported_not_guessed_v1beta1():
    api = kube({PREFIX: group("v9")})
    result = discover_resource_metrics(api, enabled=True)
    assert result["status"] == "unsupported" and result["reason"] == "metrics_version_unsupported"
    assert len(api.client.requests) == 1


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(kind="Status"), lambda d: d.update(groupVersion="other/v1beta1"),
    lambda d: d.update(resources=None), lambda d: d["resources"].append(None),
    lambda d: d["resources"].append(d["resources"][0]),
    lambda d: d["resources"][0].update(kind="Secret"),
    lambda d: d["resources"][0].update(namespaced=False),
    lambda d: d["resources"][0].update(namespaced=1),
    lambda d: d["resources"][0].update(verbs="list"),
    lambda d: d["resources"][0].update(verbs=[True]),
])
def test_resource_discovery_rejects_bad_schemas_and_scope(mutate):
    data = resources()
    mutate(data)
    api = kube({PREFIX: group(), f"{PREFIX}/v1beta1": data})
    result = collect_resource_metrics(api, enabled=True, now=NOW)
    assert result["status"] == "malformed" and result["pods"]["items"] == result["nodes"]["items"] == []
    assert len(api.client.requests) == 2


def test_resource_list_capabilities_are_checked_before_requests():
    data = resources()
    data["resources"][1]["verbs"] = ["get"]
    api = kube({PREFIX: group(), f"{PREFIX}/v1beta1": data, f"{PREFIX}/v1beta1/pods": page([sample()])})
    result = collect_resource_metrics(api, enabled=True, now=NOW)
    assert result["status"] == "partial" and result["pods"]["status"] == "ok"
    assert result["nodes"]["status"] == "unsupported" and result["nodes"]["reason"] == "list_unsupported"
    assert len(api.client.requests) == 3


@pytest.mark.parametrize("http,status,reason", [
    (403, "forbidden", "rbac_forbidden"), (404, "unsupported", "api_unsupported"),
    (405, "unsupported", "api_unsupported"), (501, "unsupported", "api_unsupported"),
    (503, "error", "request_failed"), (429, "error", "request_failed"), (401, "error", "authentication_failed"),
])
def test_metrics_discovery_error_categories_are_safe_and_do_not_fall_back(http, status, reason):
    api = kube({PREFIX: HcsError(http, "private-code", "private-body")})
    result = collect_resource_metrics(api, enabled=True, now=NOW)
    assert result["status"] == result["pods"]["status"] == result["nodes"]["status"] == status
    assert result["pods"]["reason"] == reason and len(api.client.requests) == 1
    assert "private" not in repr(result)


def test_cpu_cores_memory_working_set_bytes_and_full_pod_aggregation_are_exact():
    pod = sample(cpu="9559630n", memory="22244Ki")
    pod["containers"].append({"name": "sidecar", "usage": {"cpu": "5m", "memory": "1.5Mi"}})
    result = collect_resource_metrics(metrics_kube([pod]), enabled=True, now=NOW)
    row = result["pods"]["items"][0]
    assert result["status"] == row["status"] == "ok" and row["fresh"] is True
    assert row["cpu_cores"] == pytest.approx(0.014559630, abs=1e-12)
    assert row["memory_bytes"] == Decimal(22244 * 1024 + 1572864)
    assert row["window_seconds"] == Decimal(30) and row["age_seconds"] == Decimal(15)
    assert result["nodes"]["items"][0]["cpu_cores"] == Decimal("0.125")
    assert "percent" not in repr(result)


def test_only_explicit_valid_zeros_can_produce_zero_usage():
    result = collect_resource_metrics(metrics_kube([sample(cpu="0n", memory="0Ki")]), enabled=True, now=NOW)
    assert result["pods"]["items"][0]["cpu_cores"] == Decimal(0)
    assert result["pods"]["items"][0]["memory_bytes"] == Decimal(0)
    empty = collect_resource_metrics(metrics_kube([], []), enabled=True, now=NOW)
    assert empty["status"] == "ok" and empty["pods"]["items"] == []
    assert empty["pods"]["reason"] == "metrics_list_empty" and empty["pods"]["complete"] is True


def test_invalid_container_quantity_keeps_other_resource_but_not_partial_sum():
    pod = sample()
    pod["containers"].append({"name": "sidecar", "usage": {"cpu": "invalid", "memory": "1Mi"}})
    result = collect_resource_metrics(metrics_kube([pod, sample(name="good")]), enabled=True, now=NOW)
    bad, good = result["pods"]["items"]
    assert result["status"] == "partial" and result["pods"]["complete"] is True
    assert bad["cpu_cores"] is None and bad["memory_bytes"] == Decimal(1611661312)
    assert bad["containers"][0]["cpu_cores"] == Decimal("0.125")
    assert bad["containers"][1]["cpu_cores"] is None and good["status"] == "ok"


@pytest.mark.parametrize("containers,reason", [
    (None, "containers_invalid"), ([], "containers_invalid"), ({}, "containers_invalid"),
    ([None], "containers_invalid"), ([{"name": ""}], "containers_invalid"),
    ([{"name": "same"}, {"name": "same"}], "containers_duplicate"),
])
def test_missing_or_duplicate_containers_are_unknown_not_empty_sum(containers, reason):
    result = collect_resource_metrics(metrics_kube([sample(containers=containers)]), enabled=True, now=NOW)
    row = result["pods"]["items"][0]
    assert row["status"] == "malformed" and row["reason"] == reason
    assert row["cpu_cores"] is row["memory_bytes"] is None and result["nodes"]["status"] == "ok"


@pytest.mark.parametrize("usage", [None, {}, [], {"cpu": 0}, {"cpu": "NaN", "memory": "bad"}])
def test_malformed_node_usage_does_not_become_zero_or_block_pods(usage):
    result = collect_resource_metrics(metrics_kube(nodes=[sample("nodes", usage=usage)]), enabled=True, now=NOW)
    node = result["nodes"]["items"][0]
    assert result["pods"]["status"] == "ok" and node["status"] == "partial"
    assert node["cpu_cores"] is node["memory_bytes"] is None


@pytest.mark.parametrize("fields,reason", [
    ({"timestamp": None}, "timestamp_invalid"), ({"timestamp": "bad"}, "timestamp_invalid"),
    ({"timestamp": "2026-11-06T12:00:00"}, "timestamp_invalid"),
    ({"timestamp": "2026-11-06"}, "timestamp_invalid"),
    ({"timestamp": "2026-11-06T12:00:00+25:00"}, "timestamp_invalid"),
    ({"timestamp": "2026-11-06T12:59:45+00:60"}, "timestamp_invalid"),
    ({"timestamp": (NOW + timedelta(seconds=21)).isoformat()}, "timestamp_future"),
    ({"timestamp": (NOW - timedelta(seconds=301)).isoformat()}, "sample_stale"),
    ({"window": None}, "window_invalid"), ({"window": "0s"}, "window_invalid"),
    ({"window": "-1s"}, "window_invalid"), ({"window": "NaNs"}, "window_invalid"),
    ({"window": "30"}, "window_invalid"), ({"window": 30}, "window_invalid"),
    ({"window": "1sHIDDEN"}, "window_invalid"), ({"window": "1e3s"}, "window_invalid"),
    ({"window": "301s"}, "window_excessive"), ({"window": "24h"}, "window_excessive"),
])
def test_time_evidence_is_required_for_both_resources_no_stale_values(fields, reason):
    result = collect_resource_metrics(metrics_kube([sample(**fields)], [sample("nodes", **fields)]),
                                      enabled=True, now=NOW)
    for kind in ("pods", "nodes"):
        row = result[kind]["items"][0]
        assert row["reason"] == reason and row["fresh"] is False and row["status"] == "partial"
        assert row["cpu_cores"] is row["memory_bytes"] is None


@pytest.mark.parametrize("timestamp", [
    "2026-11-06T11:55:00Z", "2026-11-06T12:00:20Z", "2026-11-06T20:59:45+09:00",
    "2026-11-06T11:59:45.123456789Z",
])
def test_timestamps_utc_offset_nanoseconds_and_boundary_freshness(timestamp):
    row = collect_resource_metrics(metrics_kube([sample(timestamp=timestamp)]), enabled=True, now=NOW)["pods"]["items"][0]
    assert row["status"] == "ok" and row["fresh"] is True and row["timestamp"].endswith("+00:00")


@pytest.mark.parametrize("window,expected", [
    ("1m30s", "90"), ("1500ms", "1.5"), ("250000us", "0.25"),
    ("250000µs", "0.25"), ("250000μs", "0.25"), ("1ns", "0.000000001"), ("+30s", "30"),
])
def test_go_duration_windows_are_normalized_exactly(window, expected):
    row = collect_resource_metrics(metrics_kube([sample(window=window)]), enabled=True, now=NOW)["pods"]["items"][0]
    assert row["status"] == "ok" and row["window_seconds"] == pytest.approx(float(expected), rel=1e-12)


@pytest.mark.parametrize("namespace", ["../secrets", ".", "..", "ns/x", "NS", "ns?watch=true", "ns%2fx", "", "ns\n"])
def test_namespace_cannot_escape_fixed_read_only_metrics_paths(namespace):
    api = kube()
    result = collect_resource_metrics(api, enabled=True, namespace=namespace, now=NOW)
    assert result["status"] == "malformed" and result["reason"] == "namespace_invalid"
    assert not api.client.requests


def test_namespace_pod_scope_node_cluster_scope_and_independent_failures():
    api = metrics_kube(namespace="ns")
    api.client.responses[f"{PREFIX}/v1beta1/namespaces/ns/pods"] = HcsError(403, "Forbidden", "synthetic")
    result = collect_resource_metrics(api, enabled=True, namespace="ns", now=NOW)
    assert result["status"] == "partial" and result["pods"]["status"] == "forbidden"
    assert result["pods"]["items"] == [] and result["nodes"]["status"] == "ok"
    assert [r[1] for r in api.client.requests][-2:] == [f"{PREFIX}/v1beta1/namespaces/ns/pods", f"{PREFIX}/v1beta1/nodes"]


@pytest.mark.parametrize("metadata", [{}, {"name": "worker"}, {"name": "worker", "namespace": "other"},
                                      {"name": "worker", "namespace": "ns", "uid": []}])
def test_invalid_metric_identity_and_foreign_namespace_keep_unknown(metadata):
    result = collect_resource_metrics(metrics_kube([sample(metadata=metadata)], namespace="ns"),
                                      enabled=True, namespace="ns", now=NOW)
    row = result["pods"]["items"][0]
    assert row["status"] == "malformed" and row["reason"] == "metric_identity_invalid"
    assert row["cpu_cores"] is row["memory_bytes"] is None


def test_duplicate_metric_identity_poison_neither_first_nor_last_valid_peer():
    result = collect_resource_metrics(metrics_kube([sample(cpu="1"), sample(cpu="2"), sample(name="peer")]),
                                      enabled=True, now=NOW)
    a, b, peer = result["pods"]["items"]
    assert a["reason"] == b["reason"] == "metric_identity_duplicate"
    assert a["cpu_cores"] is b["cpu_cores"] is None and peer["status"] == "ok"


@pytest.mark.parametrize("fields", [{"kind": "Secret"}, {"apiVersion": "metrics.k8s.io/v999"}])
def test_metric_response_type_must_match_negotiated_resource(fields):
    item = sample()
    item.update(fields)
    row = collect_resource_metrics(metrics_kube([item]), enabled=True, now=NOW)["pods"]["items"][0]
    assert row["reason"] == "metric_type_invalid" and row["cpu_cores"] is None


def test_supported_v1_schema_is_actually_used_for_lists_and_samples():
    pod, node = sample(apiVersion="metrics.k8s.io/v1"), sample("nodes", apiVersion="metrics.k8s.io/v1")
    api = metrics_kube([pod], [node], version="v1")
    assert collect_resource_metrics(api, enabled=True, now=NOW)["status"] == "ok"
    assert all("v1beta1" not in path for _, path, _ in api.client.requests)


def test_guarded_metrics_paging_uses_opaque_tokens_and_consistent_resource_version():
    token = "opaque+/=not-a-path"
    pages = [page([sample()], token=token), page([sample(name="second")])]
    api = metrics_kube()
    api.client.responses[f"{PREFIX}/v1beta1/pods"] = lambda _: pages.pop(0)
    result = collect_resource_metrics(api, enabled=True, now=NOW)
    requests = [kw for _, path, kw in api.client.requests if path.endswith("/pods")]
    assert result["status"] == "ok" and len(result["pods"]["items"]) == 2
    assert requests == [{"params": {"limit": 500}, "host_vars": {"cluster_id": "synthetic-cluster"}},
                        {"params": {"limit": 500, "continue": token}, "host_vars": {"cluster_id": "synthetic-cluster"}}]


@pytest.mark.parametrize("second", [
    HcsError(410, "Expired", "synthetic"), page(version="different"), page(token="same"),
    page(token=[]), {"items": None}, {"error_code": "Error", "items": []},
])
def test_paging_failure_discards_partial_snapshot_but_other_source_still_works(second):
    pages = [page([sample()], token="same"), second]
    api = metrics_kube()
    api.client.responses[f"{PREFIX}/v1beta1/pods"] = lambda _: pages.pop(0)
    result = collect_resource_metrics(api, enabled=True, now=NOW)
    assert result["pods"]["status"] in ("error", "partial", "malformed")
    assert result["pods"]["items"] == [] and result["pods"]["complete"] is False
    assert result["nodes"]["status"] == "ok"


def test_owner_lists_also_use_guarded_pagination_and_never_use_partial_intermediates():
    middle = obj("ReplicaSet", "deployment-random", "rs-uid", references=[owner("Deployment", "top", "top-uid")])
    pages = [page([middle], token="more"), page(version="changed")]
    api = kube({"/apis/apps/v1/replicasets": lambda _: pages.pop(0)})
    row = resolve_pod_owners(api, [obj(references=[owner()])], enabled=True)["items"][0]
    assert row["status"] == "partial" and row["reason"] == "incomplete_list" and row["top_owner"] is None


@pytest.mark.parametrize("kwargs", [
    {"now": NOW.replace(tzinfo=None)}, {"max_age": timedelta(0)}, {"max_age": "5m"},
    {"future_tolerance": timedelta(seconds=-1)}, {"max_window": timedelta(0)},
])
def test_invalid_caller_time_policy_fails_before_any_cloud_requests(kwargs):
    api = kube()
    with pytest.raises(ValueError, match="native_metrics_time_policy_invalid"):
        collect_resource_metrics(api, enabled=True, **kwargs)
    assert not api.client.requests


def test_public_results_and_module_logs_never_expose_upstream_messages_or_payloads(caplog):
    secret_marker = "PRIVATE-UPSTREAM-PAYLOAD-SHOULD-NOT-APPEAR"
    api = metrics_kube()
    api.client.responses[f"{PREFIX}/v1beta1/pods"] = HcsError(503, secret_marker, secret_marker)
    with caplog.at_level("INFO", logger="app.k8s.native_capabilities"):
        result = collect_resource_metrics(api, enabled=True, now=NOW)
    assert caplog.records and all(r.message == "cce_native_capability" for r in caplog.records)
    assert secret_marker not in repr(result) and secret_marker not in repr([r.__dict__ for r in caplog.records])
    assert all(r.reason in ("metrics_discovered", "metrics_partial") for r in caplog.records)
    assert all("worker" not in repr(r.__dict__) for r in caplog.records)


def test_results_can_be_stored_in_json_snapshot_without_custom_encoder_or_decimal_loss_to_zero():
    result = collect_resource_metrics(metrics_kube([sample(cpu="1n", memory="400m")]), enabled=True, now=NOW)
    stored = json.loads(json.dumps(result, allow_nan=False))
    assert stored["pods"]["items"][0]["cpu_cores"] == 1e-9
    assert stored["pods"]["items"][0]["memory_bytes"] == 0.4
    assert stored == result
    resolved = resolve_pod_owners(kube(), [obj()], enabled=True)
    assert json.loads(json.dumps(resolved, allow_nan=False)) == resolved


@pytest.mark.parametrize("timestamp", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_out_of_range_utc_conversion_is_unknown_without_aborting_metrics(timestamp):
    result = collect_resource_metrics(metrics_kube([sample(timestamp=timestamp)]), enabled=True, now=NOW)
    assert result["pods"]["items"][0]["reason"] == "timestamp_invalid" and result["nodes"]["status"] == "ok"


def test_sub_nanosecond_window_does_not_create_unreal_sampling_interval():
    row = collect_resource_metrics(metrics_kube([sample(window="0.1ns")]), enabled=True, now=NOW)["pods"]["items"][0]
    assert row["reason"] == "window_invalid" and row["cpu_cores"] is None


@pytest.mark.parametrize("pod", [None, {}, {"metadata": None}, obj(uid=""), obj(namespace="other/ns"),
                                {**obj(), "kind": "ReplicaSet"}])
def test_invalid_supplied_pod_does_not_prevent_other_pod_resolution(pod):
    result = resolve_pod_owners(kube(), [pod, obj(name="valid-peer")], enabled=True)
    invalid, good = result["items"]
    assert invalid["reason"] == "pod_identity_invalid" and invalid["pod"] is None
    assert good["status"] == "ok" and good["pod"]["name"] == "valid-peer"


def test_an_invalid_intermediate_does_not_hide_other_verified_workload():
    middle = obj("ReplicaSet", "deployment-random", "rs-uid", references=[owner("Deployment", "top", "top-uid")])
    api = kube({"/apis/apps/v1/replicasets": page([middle, {}])})
    result = resolve_pod_owners(api, [obj(references=[owner()])], enabled=True)
    assert result["status"] == "partial" and result["sources"]["ReplicaSet"]["reason"] == "owner_list_invalid_items"
    assert result["items"][0]["reason"] == "owner_chain_verified"


def test_namespace_never_matches_owner_of_same_uid_in_different_namespace():
    direct = owner()
    wrong = obj("ReplicaSet", direct["name"], direct["uid"], namespace="other",
                references=[owner("Deployment", "foreign", "foreign-uid")])
    api = kube({"/apis/apps/v1/replicasets": page([wrong])})
    result = resolve_pod_owners(api, [obj(references=[direct])], enabled=True)
    assert result["items"][0]["top_owner"] is None and result["items"][0]["reason"] == "owner_not_found"


def test_declared_empty_resource_discovery_is_unsupported_and_does_not_query_metrics():
    data = resources()
    data["resources"] = []
    api = kube({PREFIX: group(), f"{PREFIX}/v1beta1": data})
    result = collect_resource_metrics(api, enabled=True, now=NOW)
    assert result["status"] == "unsupported" and len(api.client.requests) == 2


def test_discovery_without_optional_preferred_version_still_uses_advertised_known_schema():
    data = group("v1beta1")
    del data["preferredVersion"]
    api = kube({PREFIX: data, f"{PREFIX}/v1beta1": resources()})
    assert discover_resource_metrics(api, enabled=True)["version"] == "v1beta1"


def test_metric_identity_is_namespace_scoped_and_node_namespace_is_invalid():
    result = collect_resource_metrics(metrics_kube([sample(), sample(namespace="other")],
                                                 [sample("nodes", metadata={"name": "node", "namespace": "ns"})]),
                                      enabled=True, now=NOW)
    assert all(row["status"] == "ok" for row in result["pods"]["items"])
    assert result["nodes"]["status"] == "malformed"


def test_native_extension_does_not_mutate_any_input_response_objects():
    api = metrics_kube()
    before = deepcopy(api.client.responses)
    collect_resource_metrics(api, enabled=True, now=NOW)
    assert api.client.responses == before


def test_custom_freshness_policy_is_applied_instead_of_hardcoded_five_minutes():
    api = metrics_kube([sample(timestamp=(NOW - timedelta(seconds=31)).isoformat())])
    result = collect_resource_metrics(api, enabled=True, now=NOW, max_age=timedelta(seconds=30))
    assert result["pods"]["items"][0]["reason"] == "sample_stale"
    assert result["nodes"]["items"][0]["status"] == "ok"
