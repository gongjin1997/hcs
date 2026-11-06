"""CCE PDF 管理面合同、失败隔离、时效及只读访问。全部使用合成数据。"""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select

from app.api.v1.cce import router
from app.collectors.base import RunContext
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_current_user, get_scope
from app.core.response import BizError
from app.hcs import HcsError
from app.hcs.cce_inventory import CLUSTERS_PATH, cluster_info, node_info, objects, pool_info
from app.ledger.fetchers import fetch_cce
from app.models import CceClusterSnapshot, HcsAccount, InspectionTask, Resource
from app.schemas.cce import CceSettings
from app.services import cce_monitoring as service
from app.services import cce_settings

NOW = datetime(2026, 11, 6, 10, tzinfo=UTC)


def cluster(cid="cluster-a"):
    return {"metadata": {"uid": cid, "name": cid, "annotations": {
        "totalNodesNumber": "2", "activeNodesNumber": "1", "totalNodesCPU": "8", "totalNodesMemory": "16384",
        "installedAddonInstances": '[{"addonTemplateName":"metrics-server","version":"1","status":"running"}]'}},
        "spec": {"version": "v1.25", "flavor": "small"},
        "status": {"phase": "Available", "endpoints": [{"type": "Internal", "url": "https://192.0.2.1:5443"}]}}


def node(age=0, phase="Active"):
    return {"metadata": {"uid": "node-a", "name": "node-a"}, "spec": {}, "status": {
        "phase": phase, "serverId": "server-a", "privateIP": "192.0.2.5",
        "lastProbeTime": (NOW - timedelta(seconds=age)).isoformat()}}


class Client:
    def __init__(self, failed=None, listed=None):
        self.failed, self.listed, self.requests = failed, listed if listed is not None else [cluster()], []

    def get(self, service, path, **kwargs):
        self.requests.append((service, path, deepcopy(kwargs)))
        if self.failed and self.failed in path:
            raise HcsError(503, "TEST.UNAVAILABLE", "synthetic response")
        if path == CLUSTERS_PATH:
            return {"items": self.listed}
        if path.endswith("/nodes"):
            return {"items": [node()]}
        if path.endswith("/nodepools"):
            return {"items": [{"metadata": {"uid": "default", "name": "DefaultPool"}, "status": {
                "currentNode": 1, "creatingNode": 0, "deletingNode": 0,
                "conditions": [{"type": "Scalable", "status": "False"}]}}]}
        raise AssertionError(path)


def context(db, client=None, tenant=1, project=10):
    cce_settings.save(db, tenant, CceSettings(enabled=True), "test")
    return RunContext(client=client or Client(), account="synthetic", project_id="project",
                      db=db, tenant_id=tenant, project_ref=project)


def scope(tenants=None, permissions=None):
    user = CurrentUser(id=1, username="synthetic", display_name="synthetic", tenant_ids=tenants,
                       permissions=permissions or ["podevent:view"])
    return Scope(tenants, tenants is None, user)


@pytest.mark.parametrize("phase", ["Active", "Abnormal", "Error"])
@pytest.mark.parametrize("age,expected", [(299, "fresh"), (300, "fresh"), (301, "stale"), (-1, "unknown")])
def test_node_pdf_five_minute_boundary(phase, age, expected):
    info = node_info(node(age, phase), NOW)
    assert info["freshness"] == expected
    assert info["health"] == ("healthy" if phase == "Active" else "abnormal") if expected == "fresh" else (
        info["health"] == "unknown")
    assert info["last_probe_at"] == (NOW - timedelta(seconds=age)).isoformat()


@pytest.mark.parametrize("stamp", [None, "bad", "2026-11-06T10:00:00", 0])
def test_missing_probe_time_is_never_healthy(stamp):
    raw = node()
    raw["status"]["lastProbeTime"] = stamp
    assert node_info(raw, NOW)["health"] == "unknown"


def test_capacity_annotations_and_endpoint_preserve_units_and_ports():
    info = cluster_info(cluster())
    assert info["capacity"] == {"total_nodes": 2, "active_nodes": 1, "total_cpu_raw": "8",
                                "total_memory_raw": "16384", "memory_unit": "unverified"}
    assert info["endpoints"][0]["port"] == 5443
    assert info["addons"][0]["name"] == "metrics-server"
    raw = cluster()
    raw["status"]["endpoints"] = [{"url": "https://user:secret@example.invalid?token=secret"}]
    raw["status"]["message"] = "Bearer sensitive-value"
    info = cluster_info(raw)
    assert info["endpoints"] == [] and "sensitive-value" not in info["message"]


def test_pool_conditions_do_not_invent_error_from_scalable_false():
    info = pool_info({"metadata": {"uid": "p"}, "status": {"phase": "Error", "currentNode": 2,
                     "conditions": [{"type": "Scalable", "status": "False"}]}})
    assert "phase" not in info and "health" not in info
    assert info["current_nodes"] == 2 and info["creating_nodes"] is None
    assert info["conditions"][0]["status"] == "False"


def test_optional_nodepool_probe_404_is_warning_not_collection_failure():
    from app.tools.probe import PROBES, Runner

    descriptor = next(item for item in PROBES if item.code == "cce.nodepools")

    class MissingNodePools:
        def get(self, *_args, **_kwargs):
            raise HcsError(404, "APIGW.0101", "synthetic unsupported optional path")

    runner = Runner(MissingNodePools(), object(), 5, False)
    runner.ctx["cce_cluster"] = "cluster-a"
    result = runner.run(descriptor)
    assert descriptor.capability is True
    assert result["result"] == "WARN" and result["message"] == "上游请求失败（HTTP 404，APIGW.0101）"


@pytest.mark.parametrize("data", [{"items": [{}]}, {"items": [cluster(), cluster()]},
                                  {"items": [], "metadata": {"continue": "next"}}, {}])
def test_management_snapshot_identity_and_completeness(data):
    with pytest.raises(HcsError):
        objects(data, "集群")


def test_default_disabled_has_no_cloud_requests(mem_db):
    client = Client()
    ctx = RunContext(client=client, account="synthetic", project_id="p", db=mem_db, tenant_id=1, project_ref=10)
    assert service.CceHealthCollector().collect(ctx) == []
    assert client.requests == [] and ctx.stats["cce_skipped"] == "disabled"


def test_collects_default_pool_and_separate_freshness(mem_db, monkeypatch):
    monkeypatch.setattr(service, "utcnow", lambda: NOW)
    ctx = context(mem_db)
    service.CceHealthCollector().collect(ctx)
    assert ctx.client.requests[0][2] == {"params": {"detail": "true"}}
    pool_call = next(request for request in ctx.client.requests if request[1].endswith("/nodepools"))
    assert pool_call[2] == {"params": {"showDefaultNodePool": "true"}}
    row = mem_db.scalar(select(CceClusterSnapshot))
    assert row is not None and row.nodepools[0]["id"] == "default"
    assert service.serialize(mem_db, row)["nodes"][0]["health"] == "healthy"
    monkeypatch.setattr(service, "utcnow", lambda: NOW + timedelta(seconds=301))
    data = service.serialize(mem_db, row)
    assert data["freshness"] == "stale" and data["nodes"][0]["health"] == "unknown"


def test_failed_node_enrichment_keeps_both_cluster_identities(mem_db, monkeypatch):
    monkeypatch.setattr(service, "utcnow", lambda: NOW)
    client = Client("cluster-b/nodes", [cluster(), cluster("cluster-b")])
    ctx = context(mem_db, client)
    service.CceHealthCollector().collect(ctx)
    rows = list(mem_db.scalars(select(CceClusterSnapshot).order_by(CceClusterSnapshot.cluster_id)))
    assert len(rows) == 2 and rows[0].nodes and rows[1].nodes == []
    assert rows[1].sources["nodes"]["state"] == "error" and ctx.errors
    ledger_ctx = {}
    records = fetch_cce(Client("cluster-b/nodes", [cluster(), cluster("cluster-b")]), ledger_ctx)
    assert len(records) == 2 and records[0].attrs["node_count"] == 1
    assert records[1].attrs["node_count"] is None and "node_count" in records[1].clear_attrs
    assert ledger_ctx["cce_node_failures"] == 1


def test_failed_refresh_preserves_last_success_but_invalidates_current_health(mem_db, monkeypatch):
    monkeypatch.setattr(service, "utcnow", lambda: NOW)
    service.CceHealthCollector().collect(context(mem_db))
    row = mem_db.scalar(select(CceClusterSnapshot))
    service.CceHealthCollector().collect(context(mem_db, Client("/nodes")))
    assert row.nodes and row.sources["nodes"]["succeeded_at"] == NOW.isoformat()
    assert service.serialize(mem_db, row)["nodes"][0]["health"] == "unknown"
    service.CceHealthCollector().collect(context(mem_db, Client(CLUSTERS_PATH)))
    assert not row.missing and service.serialize(mem_db, row)["freshness"] == "refresh_failed"


def test_scope_and_ecs_association_never_cross_project_or_tenant(mem_db, monkeypatch):
    monkeypatch.setattr(service, "utcnow", lambda: NOW)
    for tenant, project in [(1, 10), (1, 11), (2, 10)]:
        service.CceHealthCollector().collect(context(mem_db, tenant=tenant, project=project))
    mem_db.add(Resource(resource_type="ecs_server", resource_id="server-a", name="wrong-project", service="ecs",
                        tenant_id=1, project_ref=11))
    mem_db.flush()
    listed = service.list_clusters(mem_db, scope([1]), project_ref=10)
    assert listed["total"] == 1
    detail = service.cluster_detail(mem_db, scope([1]), listed["items"][0]["key"])
    assert detail["nodes"][0]["ecs_resource_ref"] is None
    with pytest.raises(BizError) as exc:
        service.cluster_detail(mem_db, scope([1]), service.snapshot_key(2, 10, "cluster-a"))
    assert exc.value.http_status == 404


def test_new_task_and_settings_do_not_enable_existing_gates(mem_db):
    from app.services.bootstrap import ensure_tasks

    ensure_tasks(mem_db)
    task = mem_db.get(InspectionTask, cce_settings.TASK_CODE)
    assert task is not None and not task.enabled
    cce_settings.save(mem_db, 1, CceSettings(enabled=True, resource_metrics=True), "test")
    assert not cce_settings.status(mem_db, 1)["effective_enabled"]
    assert not cce_settings.effective(mem_db, 2).enabled


def test_native_opt_in_roundtrip_and_read_time_expiry(mem_db, monkeypatch):
    monkeypatch.setattr(service, "utcnow", lambda: NOW)
    ctx = context(mem_db)
    cce_settings.save(mem_db, 1, CceSettings(enabled=True, resource_metrics=True, resolve_owners=True), "test")
    original_get = ctx.client.get
    calls = []

    def fetch(service_name, path, **kwargs):
        calls.append((service_name, path, kwargs))
        if service_name == "cce":
            return original_get(service_name, path, **kwargs)
        assert service_name == "cce_k8s" and kwargs["host_vars"] == {"cluster_id": "cluster-a"}
        if path == "/apis/metrics.k8s.io":
            return {"kind": "APIGroup", "name": "metrics.k8s.io",
                    "versions": [{"groupVersion": "metrics.k8s.io/v1beta1", "version": "v1beta1"}],
                    "preferredVersion": {"groupVersion": "metrics.k8s.io/v1beta1", "version": "v1beta1"}}
        if path == "/apis/metrics.k8s.io/v1beta1":
            return {"kind": "APIResourceList", "groupVersion": "metrics.k8s.io/v1beta1", "resources": [
                {"name": "nodes", "kind": "NodeMetrics", "namespaced": False, "verbs": ["get", "list"]},
                {"name": "pods", "kind": "PodMetrics", "namespaced": True, "verbs": ["get", "list"]}]}
        if path == "/apis/metrics.k8s.io/v1beta1/nodes":
            return {"items": [{"metadata": {"name": "node-a"}, "timestamp": NOW.isoformat(), "window": "30s",
                               "usage": {"cpu": "125m", "memory": "64Mi"}}]}
        if path == "/apis/metrics.k8s.io/v1beta1/pods":
            return {"items": []}
        if path == "/api/v1/pods":
            return {"items": [{"metadata": {"name": "web-a", "namespace": "default", "uid": "pod-a",
                      "ownerReferences": [{"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "web-rs",
                                           "uid": "rs-a", "controller": True}]}}]}
        if path == "/apis/apps/v1/replicasets":
            return {"items": [{"apiVersion": "apps/v1", "kind": "ReplicaSet",
                     "metadata": {"name": "web-rs", "namespace": "default", "uid": "rs-a",
                      "ownerReferences": [{"apiVersion": "apps/v1", "kind": "Deployment", "name": "web",
                                           "uid": "deployment-a", "controller": True}]}}]}
        raise AssertionError(path)

    ctx.client.get = fetch
    service.CceHealthCollector().collect(ctx)
    row = mem_db.scalar(select(CceClusterSnapshot))
    mem_db.flush()
    mem_db.expire(row)
    assert row.native["owners"]["items"][0]["top_owner"]["uid"] == "deployment-a"
    sample = row.native["metrics"]["nodes"]["items"][0]
    assert sample["cpu_cores"] == 0.125 and sample["memory_bytes"] == 64 * 1024**2 and sample["fresh"]
    assert not ctx.errors and all(service_name in ("cce", "cce_k8s") for service_name, _, _ in calls)
    monkeypatch.setattr(service, "utcnow", lambda: NOW + timedelta(seconds=301))
    data = service.serialize(mem_db, row)
    assert not data["native"]["metrics"]["nodes"]["items"][0]["fresh"]
    assert row.native["metrics"]["nodes"]["items"][0]["fresh"]  # read does not rewrite the historical sample


def test_disabled_tenant_does_not_construct_cloud_client(mem_db, monkeypatch):
    from app.services import pipeline
    from app.services.tenancy import Target

    target = Target(tenant_id=1, code="test", name="test", short_name="test", project_ref=10,
                    project_id="test", project_name="test", region="test")
    monkeypatch.setattr(pipeline, "resolve_targets", lambda *_: [target])
    monkeypatch.setattr(Target, "client", lambda *_args, **_kwargs: pytest.fail("disabled tenant requested cloud client"))
    assert pipeline.run_all(mem_db, "cce_cluster_health") == []


def test_readonly_api_and_real_xlsx(mem_db, monkeypatch):
    monkeypatch.setattr(service, "utcnow", lambda: NOW)
    mem_db.add(HcsAccount(id=1, code="synthetic", name="synthetic", iam_endpoint="https://example.invalid", domain_name="d"))
    service.CceHealthCollector().collect(context(mem_db))
    app = FastAPI()
    app.add_exception_handler(BizError, lambda _request, exc: JSONResponse(
        status_code=exc.http_status, content={"code": exc.code, "message": exc.message}))
    app.include_router(router)
    user_scope = scope([1])
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_scope] = lambda: user_scope
    app.dependency_overrides[get_current_user] = lambda: user_scope.user
    with TestClient(app) as api:
        assert api.get("/cce/clusters").status_code == 200
        key = service.snapshot_key(1, 10, "cluster-a")
        data = api.get(f"/cce/clusters/{key}")
        assert data.status_code == 200 and data.json()["data"]["nodes"][0]["id"] == "node-a"
        response = api.get(f"/cce/clusters/{key}/export")
        assert response.status_code == 200 and response.content[:2] == b"PK"
        wb = load_workbook(BytesIO(response.content))
        assert wb.sheetnames == ["采集信息", "节点", "节点池", "原生节点指标", "原生Pod指标", "工作负载归属"]
        assert wb["节点"].cell(2, 1).value == "node-a"
        assert api.put("/cce/settings/1", json={"enabled": True}).status_code == 403
