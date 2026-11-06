"""上传 PDF 的共享调用链：台账完整性、版本单位、探针和原生作业状态。"""
from io import BytesIO
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.collectors.base import Observation, RunContext
from app.core.config import get_settings
from app.hcs.errors import HcsError
from app.ledger.fetchers import fetch_cce, fetch_ecs, fetch_gaussdb, fetch_mrs, gaussdb_attrs
from app.models import HcsProject, Resource
from app.models.base import utcnow
from app.services import tenant_check
from app.services.pipeline import upsert_resource
from app.tools.probe import PROBES, Runner
from tests.test_job_reports import report, resource
from tests.test_job_reports import report_api as report_fixture

report_api = report_fixture


@pytest.mark.parametrize("total", [None, True, -1, "1"])
def test_ecs_ledger_requires_a_documented_integer_count(total):
    body = {"servers": []}
    if total is not None:
        body["count"] = total
    client = SimpleNamespace(get=lambda service, *args, **kwargs: body if service == "ecs" else {"volumes": []})
    with pytest.raises(HcsError):
        fetch_ecs(client, {})


@pytest.mark.parametrize("continuation", ["next", 0, False, [], {}])
def test_cce_ledger_rejects_unverified_partial_cluster_list(continuation):
    def get(_service, path, **kwargs):
        rows = [{"metadata": {"uid": "cluster-1", "name": "cluster"}, "spec": {}, "status": {}}]
        return {"items": rows, "metadata": {"continue": continuation}}

    with pytest.raises(HcsError):
        fetch_cce(SimpleNamespace(get=get), {})


@pytest.mark.parametrize("continuation", ["next", 0, False, [], {}])
def test_cce_ledger_keeps_cluster_identity_when_node_enrichment_is_incomplete(continuation):
    def get(_service, path, **kwargs):
        if path.endswith("nodes"):
            return {"items": [], "metadata": {"continue": continuation}}
        return {"items": [{"metadata": {"uid": "cluster-1", "name": "cluster"}, "spec": {}, "status": {}}]}

    ctx = {}
    record, = fetch_cce(SimpleNamespace(get=get), ctx)
    assert record.resource_id == "cluster-1"
    assert record.attrs["node_count"] is None and record.spec.endswith("未知 节点")
    assert record.clear_attrs == ("node_count",) and ctx["cce_node_failures"] == 1


@pytest.mark.parametrize("total", [None, True, -1, 1.5, "invalid"])
def test_mrs_ledger_requires_documented_total(total):
    body = {"clusters": []}
    if total is not None:
        body["clusterTotal"] = total
    client = SimpleNamespace(get=lambda *args, **kwargs: body)
    with pytest.raises(HcsError):
        fetch_mrs(client, {})


@pytest.mark.parametrize("field,value", [("clusterId", ""), ("clusterId", None), ("clusterName", ""),
                                         ("clusterState", []), ("clusterState", True)])
def test_mrs_ledger_rejects_invalid_identity_or_state(field, value):
    row = {"clusterId": "cluster-1", "clusterName": "cluster", "clusterState": "running", field: value}
    client = SimpleNamespace(get=lambda *args, **kwargs: {"clusters": [row], "clusterTotal": "1"})
    with pytest.raises(HcsError):
        fetch_mrs(client, {})


def test_mrs_ledger_rejects_cross_page_duplicate_even_if_page_contents_differ():
    def get(_service, _path, *, params):
        return {"clusters": [{"clusterId": "same", "clusterName": "cluster", "clusterState":
                              "running" if params["currentPage"] == 1 else "failed"}], "clusterTotal": "2"}
    with pytest.raises(HcsError):
        fetch_mrs(SimpleNamespace(get=get), {})


@pytest.mark.parametrize("variant", ["v3.3", "v3.2", "v3.1", "opengauss_v3"])
def test_gaussdb_memory_unit_is_verified_only_for_documented_v31(variant, monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_gaussdb_api_variant", variant)
    body = {"instances": [{"id": "instance-1", "name": "db", "type": "centralization_standard",
                           "flavor_info": {"vcpu": "8", "mem": "32"}, "volume": {"size": 100}}], "total_count": 1}
    record = fetch_gaussdb(SimpleNamespace(get=lambda *args, **kwargs: body), {})[0]
    assert record.memory_gb == (32 if variant == "v3.1" else None)
    assert record.attrs["memory_unit_verified"] is (variant == "v3.1")
    assert record.storage_gb is None and record.attrs["volume_unit_verified"] is False
    assert record.attrs["type"] == "集中式"
    assert ("32 GB" in record.spec) is (variant == "v3.1")


@pytest.mark.parametrize("variant,expected", [("v3.1", 32), ("v3.3", None)])
def test_gaussdb_inspection_uses_same_memory_units_as_ledger(world, mem_db, monkeypatch, variant, expected):
    monkeypatch.setattr(get_settings(), "hcs_gaussdb_api_variant", variant)
    tenant = world["tenant-b"]
    project = mem_db.scalar(select(HcsProject).where(HcsProject.account_id == tenant.id))
    row = Resource(service="gaussdb", resource_type="gaussdb_instance", resource_id="instance-qa", name="db",
                   tenant_id=tenant.id, project_ref=project.id, memory_gb=64, storage_gb=100)
    mem_db.add(row)
    mem_db.commit()
    attrs = gaussdb_attrs({"type": "centralization_standard", "flavor_info": {"vcpu": "8", "mem": "32"}})
    observation = Observation("gaussdb_instance", "instance-qa", "db", "instance_status", {"status": "ACTIVE"}, attrs=attrs)
    context = RunContext(client=None, account=tenant.code, project_id=project.project_id,
                         tenant_id=tenant.id, project_ref=project.id)
    updated = upsert_resource(mem_db, "gaussdb", [observation], utcnow(), context)
    assert updated.id == row.id and updated.memory_gb == expected and updated.storage_gb is None


@pytest.mark.parametrize("code,key,total_key", [("mrs.clusters", "clusters", "clusterTotal"),
                                               ("dataarts.jobs", "jobs", "total"),
                                               ("dataarts.instances", "instances", "total")])
@pytest.mark.parametrize("total", [None, True, -1, 1.5, "invalid", 1])
def test_probe_does_not_report_empty_incomplete_or_invalid_totals_as_ok(code, key, total_key, total):
    body = {key: []}
    if total is not None:
        body[total_key] = total
    runner = Runner(SimpleNamespace(get=lambda *args, **kwargs: body), SimpleNamespace(), 1, False)
    runner.ctx["workspace"] = "workspace-qa"
    descriptor = next(item for item in PROBES if item.code == code)
    assert runner.run(descriptor)["result"] == "FAIL"


def test_dcs_statistic_probe_has_no_undocumented_pagination():
    descriptor = next((item for item in PROBES if item.code == "dcs.statistics"), None)
    assert descriptor is not None
    calls = []

    def get(service, path, **kwargs):
        calls.append((service, path, kwargs))
        return {"statistics": [{"instance_id": "cache-1", "used_memory": 10, "max_memory": 128}]}

    assert Runner(SimpleNamespace(get=get), SimpleNamespace(), 1, False).run(descriptor)["result"] == "OK"
    assert calls == [("dcs", "/v2/{project_id}/instances/statistic", {"params": {}})]


@pytest.mark.parametrize("body", [{}, {"items": None}, {"items": "invalid"}, {"items": []}])
def test_cce_capability_selects_available_cluster_and_validates_namespace_list(body):
    clusters = [{"metadata": {"uid": "creating"}, "status": {"phase": "Creating"}},
                {"metadata": {"uid": "available", "name": "sample"}, "status": {"phase": "Available"}}]
    calls = []

    def get(_service, _path, **kwargs):
        calls.append(kwargs)
        return body

    checks, tenant = tenant_check._Checks(), SimpleNamespace(capabilities={})
    tenant_check._cce_api(checks, tenant, SimpleNamespace(get=get), clusters)
    assert calls == [{"params": {"limit": 1}, "host_vars": {"cluster_id": "available"}}]
    assert checks.items[0]["ok"] is (body == {"items": []})


def test_probe_distinguishes_cce_management_list_from_native_k8s_paging():
    bodies = {"cce": {"items": [], "metadata": {"continue": "next"}},
              "cce_k8s": {"items": [], "metadata": {"continue": "next"}}}
    runner = Runner(SimpleNamespace(get=lambda service, *args, **kwargs: bodies[service]), SimpleNamespace(), 1, False)
    runner.ctx["cce_cluster"] = "sample"
    assert runner.run(next(item for item in PROBES if item.code == "cce.clusters"))["result"] == "FAIL"
    assert runner.run(next(item for item in PROBES if item.code == "k8s.pods"))["result"] == "OK"


@pytest.mark.parametrize("raw,display,label", [("pause", "PAUSED", "暂停"), ("manual-stop", "CANCELED", "已取消")])
def test_documented_batch_states_are_recognized_in_api_and_xlsx(mem_db, report_api, raw, display, label):
    client, _, _ = report_api
    resource(mem_db, "dgc_batch_job", raw, attrs={"instance_status": raw})
    row = report(client, "batch")["items"][0]
    assert row["status_known"] is True and row["display_status"] == display and row["status_label"] == label
    response = client.get("/api/v1/job-reports/batch/export")
    assert response.status_code == 200
    workbook = load_workbook(BytesIO(response.content), read_only=True)
    try:
        texts = [str(value) for sheet in workbook for values in sheet.values for value in values]
        assert label in texts
    finally:
        workbook.close()
