"""2026-11-03 UI 合同与 PDF 边界回归；仅内存台账和 MockTransport。"""
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.models import HcsAccount, HcsProject, Resource
from tests.test_gaussdb_monitoring import BASE, CLOUD_ID, path, slow_params
from tests.test_gaussdb_monitoring import gauss_api as gauss_api


def test_local_instance_detail_and_projects_require_only_view_and_never_open_cloud(gauss_api):
    g = gauss_api
    g.user.permissions = ["ledger:view"]
    listing = g.client.get(path("")).json()["data"]["items"]
    detail = g.client.get(f"{BASE}/instances/111")
    assert detail.status_code == 200, detail.text
    listed = next(row for row in listing if row["id"] == 111)
    assert listed["nodes"] is None and listed["component_items"] is None
    assert detail.json()["data"] == {**listed, "nodes": [], "component_items": []}
    projects = g.client.get(f"{BASE}/projects")
    assert projects.status_code == 200, projects.text
    assert projects.json()["data"] == [
        {"project_ref": 11, "project_name": "项目11", "tenant_id": 1, "tenant_name": "租户1", "region": "region-test"},
        {"project_ref": 12, "project_name": "项目12", "tenant_id": 1, "tenant_name": "租户1", "region": "region-test"},
    ]
    assert g.cloud.requests == g.cloud.contexts == []


@pytest.mark.parametrize("endpoint", ["/projects", "/instances/111"])
def test_local_helpers_cannot_bypass_view_or_header_tenant_scope(gauss_api, endpoint):
    g = gauss_api
    g.user.permissions = ["task:run"]
    assert g.client.get(BASE + endpoint).status_code == 403
    g.user.permissions = ["ledger:view"]
    assert g.client.get(BASE + endpoint, headers={"X-Tenant-Id": "2"}).status_code == 403
    assert g.cloud.requests == g.cloud.contexts == []


@pytest.mark.parametrize("change", ["foreign", "deleted", "wrong_type", "missing"])
def test_local_instance_detail_uses_active_gaussdb_and_tenant_scope(gauss_api, change):
    g = gauss_api
    resource_ref = 111
    if change == "foreign":
        resource_ref = 211
    elif change == "deleted":
        g.db.get(Resource, 111).lifecycle = "DELETED"
    elif change == "wrong_type":
        g.db.get(Resource, 111).resource_type = "dcs_instance"
    else:
        resource_ref = 999
    g.db.commit()
    assert g.client.get(f"{BASE}/instances/{resource_ref}").status_code == 404
    assert g.cloud.requests == g.cloud.contexts == []


def test_local_detail_still_works_for_disabled_or_invalid_cloud_identity(gauss_api):
    g = gauss_api
    g.user.permissions = ["ledger:view"]
    g.db.get(HcsAccount, 1).enabled = False
    g.db.get(HcsProject, 11).enabled = False
    g.db.get(Resource, 111).resource_id = "bad/local-id"
    g.db.commit()
    response = g.client.get(f"{BASE}/instances/111")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["resource_id"] == "bad/local-id"
    assert g.cloud.requests == g.cloud.contexts == []


def test_project_options_deduplicate_sort_and_skip_invalid_or_non_gauss_associations(gauss_api):
    g = gauss_api
    g.db.get(HcsProject, 11).project_name = "Z项目"
    g.db.get(HcsProject, 12).project_name = "A项目"
    for rid, pid, resource_type, lifecycle in [
        (113, 11, "gaussdb_instance", "ACTIVE"), (114, 21, "gaussdb_instance", "ACTIVE"),
        (115, None, "gaussdb_instance", "ACTIVE"), (116, 21, "dcs_instance", "ACTIVE"),
        (117, 21, "gaussdb_instance", "DELETED"),
    ]:
        g.db.add(Resource(id=rid, tenant_id=1, project_ref=pid, service="gaussdb", resource_type=resource_type,
            resource_id=f"option-{rid}", name=f"option-{rid}", lifecycle=lifecycle, health="OK"))
    g.db.commit()
    rows = g.client.get(f"{BASE}/projects").json()["data"]
    assert [r["project_ref"] for r in rows] == [12, 11]
    g.user.tenant_ids = None
    rows = g.client.get(f"{BASE}/projects", headers={"X-Tenant-Id": "2"}).json()["data"]
    assert [r["project_ref"] for r in rows] == [21] and rows[0]["tenant_name"] == "租户2"
    g.db.get(HcsProject, 21).project_name = None
    g.db.commit()
    rows = g.client.get(f"{BASE}/projects", headers={"X-Tenant-Id": "2"}).json()["data"]
    assert rows[0]["project_name"] == "project-21"
    assert g.cloud.requests == g.cloud.contexts == []


@pytest.mark.parametrize("role", ["offline", "secondary", "logger_dorado", "arbiter", "unknown", None])
def test_dn_without_primary_or_standby_role_cannot_be_a_slow_sql_candidate(gauss_api, role):
    g = gauss_api
    g.cloud.overrides["slow-sql-execute-node"] = {"nodes": [
        {"node_id": "node-11", "instance_id": CLOUD_ID, "component_type": "DN", "role": role}]}
    data = g.client.get(path("slow-nodes")).json()["data"]
    assert data["status"] == "NO_DATA" and data["items"] == []
    response = g.client.get(path("slow-sql"), params=slow_params())
    assert response.status_code == 400
    assert all(r.url.path.endswith("/slow-sql-execute-node") for r in g.cloud.requests)


@pytest.mark.parametrize("role", ["offline", "secondary", "logger_dorado", "arbiter", "unknown", None])
def test_sessions_reject_dn_non_query_roles_before_session_query(gauss_api, role):
    g = gauss_api
    body = g.cloud.default("components", 11)
    body["nodes"][0]["components"][0]["role"] = role
    g.cloud.overrides["components"] = body
    response = g.client.get(path("sessions"), params={"node_id": "node-11", "component_id": "component-11"})
    assert response.status_code == 400
    assert response.json()["code"] == 40001
    assert response.json()["message"] == "节点和 CN 或 DN 主备组件不匹配"
    assert [r.url.path.rsplit("/", 1)[-1] for r in g.cloud.requests] == ["components"]


@pytest.mark.parametrize("role", [None, ""])
@pytest.mark.parametrize("status", ["Primary", "Standby"])
def test_sessions_missing_dn_role_can_use_explicit_documented_primary_standby_status(gauss_api, role, status):
    g = gauss_api
    body = g.cloud.default("components", 11)
    body["nodes"][0]["components"][0].update(role=role, status=status)
    g.cloud.overrides["components"] = body
    response = g.client.get(path("sessions"), params={"node_id": "node-11", "component_id": "component-11"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "AVAILABLE"
    assert g.cloud.requests[-1].url.path.endswith("/real-time-session")


@pytest.mark.parametrize("role,status", [(None, "Normal"), ("", "Normal"), (None, None),
    ("offline", "Primary"), ("secondary", "Standby"), ("logger_dorado", "Primary"), ("arbiter", "Standby"),
    ("unknown", "Primary")])
def test_session_status_does_not_guess_or_override_explicit_non_query_role(gauss_api, role, status):
    g = gauss_api
    body = g.cloud.default("components", 11)
    body["nodes"][0]["components"][0].update(role=role, status=status)
    g.cloud.overrides["components"] = body
    response = g.client.get(path("sessions"), params={"node_id": "node-11", "component_id": "component-11"})
    assert response.status_code == 400 and response.json()["code"] == 40001
    assert response.json()["message"] == "节点和 CN 或 DN 主备组件不匹配"
    assert all(not r.url.path.endswith("/real-time-session") for r in g.cloud.requests)


@pytest.mark.parametrize("kind,role", [("CN", None), ("DN", "master"), ("DN", "slave")])
def test_cn_without_role_and_primary_standby_dn_remain_queryable(gauss_api, kind, role):
    g = gauss_api
    components = g.cloud.default("components", 11)
    components["nodes"][0]["components"][0].update(type=kind, role=role)
    g.cloud.overrides["components"] = components
    g.cloud.overrides["slow-sql-execute-node"] = {"nodes": [
        {"node_id": "node-11", "instance_id": CLOUD_ID, "component_type": kind, "role": role}]}
    assert g.client.get(path("slow-sql"), params=slow_params()).json()["data"]["status"] == "AVAILABLE"
    assert g.client.get(path("sessions"), params={"node_id": "node-11", "component_id": "component-11"}).json()[
        "data"]["status"] == "AVAILABLE"


def test_components_empty_page_before_declared_total_is_error_not_a_complete_catalogue(gauss_api):
    g = gauss_api
    first = g.cloud.default("components", 11)
    first["total_count"] = 2
    g.cloud.overrides["components"] = lambda request: first if request.url.params["offset"] == "0" else {
        "nodes": [], "total_count": 2}
    data = g.client.get(path("overview")).json()["data"]
    assert data["disk"]["status"] == "AVAILABLE"
    assert data["components_status"] == "ERROR" and data["components"] == []
    response = g.client.get(path("sessions"), params={"node_id": "node-11", "component_id": "component-11"})
    assert response.json()["data"]["status"] == "ERROR"
    assert all(not r.url.path.endswith("/real-time-session") for r in g.cloud.requests)


def test_inspection_empty_page_before_total_preserves_partial_status(gauss_api):
    g = gauss_api
    first = g.cloud.default("records", 11)
    first["total_count"] = 3
    g.cloud.overrides["records"] = lambda request: first if json.loads(request.content)["offset"] == 0 else {
        "records": [], "total_count": 3}
    data = g.client.get(path("inspections")).json()["data"]
    assert data["status"] == "PARTIAL" and data["truncated"] and data["total"] is None
    assert [r["record_id"] for r in data["items"]] == ["record-own"]
    assert data["warning"]


def test_repeated_foreign_inspection_page_is_detected_before_twenty_requests(gauss_api):
    g = gauss_api
    g.cloud.overrides["records"] = {"total_count": 5000, "records": [
        {"record_id": "foreign", "instance_id": "other-instance", "status": "completed"}]}
    data = g.client.get(path("inspections")).json()["data"]
    assert data["status"] == "ERROR" and data["items"] == []
    assert len(g.cloud.requests) == 2


def test_report_declared_total_marks_truncation_instead_of_claiming_complete(gauss_api):
    g = gauss_api
    body = g.cloud.default("record-own", 11)
    body["total_count"] = 3
    g.cloud.overrides["record-own"] = body
    data = g.client.get(path("inspections/record-own")).json()["data"]
    assert data["status"] == "PARTIAL" and data["truncated"] and data["total"] == 3
    assert len(data["items"]) == 2 and data["warning"]


@pytest.mark.parametrize("suffix", ["slow-sql", "slow-sql/detail"])
@pytest.mark.parametrize("start,end", [
    (datetime(2000, 1, 1, tzinfo=UTC), datetime(2000, 1, 1, 1, tzinfo=UTC)),
    (datetime(2400, 1, 1, tzinfo=UTC), datetime(2400, 1, 1, 1, tzinfo=UTC)),
    (datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC) + timedelta(microseconds=1)),
])
def test_slow_sql_requires_documented_thirteen_digit_increasing_millisecond_window(gauss_api, suffix, start, end):
    g = gauss_api
    response = g.client.get(path(suffix), params=slow_params(
        start_at=start.isoformat(), end_at=end.isoformat(), sql_id="sql-1"))
    assert response.status_code == 400, response.text
    assert g.cloud.requests == g.cloud.contexts == []


def test_component_pagination_offsets_count_nodes_even_when_a_node_has_no_components(gauss_api):
    g = gauss_api
    node = g.cloud.default("components", 11)["nodes"][0]
    first = {"id": "empty-node", "components": []}
    g.cloud.overrides["components"] = lambda request: {
        "nodes": [first if request.url.params["offset"] == "0" else node], "total_count": 2}
    data = g.client.get(path("overview")).json()["data"]
    assert data["components_status"] == "AVAILABLE" and len(data["components"]) == 1
    requests = [r for r in g.cloud.requests if r.url.path.endswith("/components")]
    assert [r.url.params["offset"] for r in requests] == ["0", "1"]


def test_project_options_are_empty_when_no_active_visible_gauss_instances_remain(gauss_api):
    g = gauss_api
    g.db.get(Resource, 111).lifecycle = "DELETED"
    g.db.get(Resource, 112).resource_type = "dcs_instance"
    g.db.commit()
    response = g.client.get(f"{BASE}/projects")
    assert response.status_code == 200 and response.json()["data"] == []
    assert g.cloud.requests == g.cloud.contexts == []


def test_report_list_totals_cannot_be_smaller_than_the_returned_page(gauss_api):
    g = gauss_api
    body = g.cloud.default("records", 11)
    body["total_count"] = 0
    g.cloud.overrides["records"] = body
    data = g.client.get(path("inspections")).json()["data"]
    assert data["status"] == "ERROR" and data["items"] == []
