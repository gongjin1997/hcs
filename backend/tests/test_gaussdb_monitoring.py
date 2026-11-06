"""GaussDB 原生面板独立契约；内存库、真实 Scope、MockTransport，正式云零调用。"""
import copy
import io
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.api.v1.gaussdb import router
from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.hcs import query_cache, ratelimit
from app.hcs.auth import AkSkAuth
from app.hcs.client import HcsClient
from app.hcs.endpoints import EndpointResolver
from app.hcs.errors import HcsError
from app.hcs.query_cache import QueryCache
from app.main import app as production_app
from app.models import HcsAccount, HcsProject, Resource
from app.services import gaussdb_monitoring as service

BASE = "/api/v1/gaussdb"
START = datetime(2026, 11, 1, 1, 2, 3, 123000, tzinfo=UTC)
END = START + timedelta(hours=1)
START_MS = int(START.timestamp() * 1000)
END_MS = int(END.timestamp() * 1000)
CLOUD_ID = "same-cloud-instance"
PERMS = ["ledger:view", "task:run", "ledger:export"]


def slow_params(**extra):
    return {"start_at": START.isoformat(), "end_at": END.isoformat(), "node_ids": ["node-11"], **extra}


def endpoint_cases():
    """枚举实际公开 GET 入口；每个入口均独立验证权限与范围，含所有导出面板。"""
    return [
        ("", {}), ("overview", {}), ("slow-nodes", {}), ("slow-sql", slow_params()),
        ("slow-sql/detail", slow_params(sql_id="sql-1")),
        ("sessions", {"node_id": "node-11", "component_id": "component-11"}),
        ("inspections", {}), ("inspections/record-own", {}),
        ("export", {"panel": "overview"}), ("export", {"panel": "slow-sql", **slow_params()}),
        ("export", {"panel": "sessions", "node_id": "node-11", "component_id": "component-11"}),
        ("export", {"panel": "inspections", "limit": 50}),
        ("export", {"panel": "inspection-detail", "record_id": "record-own"}),
    ]


def path(suffix, resource_ref=111):
    return f"{BASE}/instances/{resource_ref}/{suffix}" if suffix else f"{BASE}/instances"


class NativeCloud:
    """只替换传输与合成响应；HTTP 方法、URI、签名、参数和云请求守卫仍运行真实实现。"""

    def __init__(self):
        self.requests = []
        self.contexts = []
        self.overrides = {}

    def default(self, operation, pid):
        node = f"node-{pid}"
        if operation == "volume-usage":
            return {"used": {11: 25, 12: 50, 21: 75}[pid], "total": 100}
        if operation == "components":
            return {"total_count": 1, "nodes": [{"id": node, "name": f"节点{pid}", "components": [
                {"id": f"component-{pid}", "type": "DN", "role": "master", "status": "ACTIVE"}]}]}
        if operation == "slow-sql-execute-node":
            return {"nodes": [{"node_id": node, "node_name": f"节点{pid}", "component_type": "DN",
                               "instance_id": CLOUD_ID, "role": "master"}]}
        if operation == "slow-sql-list":
            return {"total_count": 1, "slow_sql_infos": [{"sql_id": "sql-1", "node_id": node,
                "sql_text": "SELECT 1", "user_name": "reader", "calls": 0,
                "avg_exec_time": 1500000, "avg_cpu_time": 0, "avg_io_time": None}]}
        if operation == "slow-sql-detail":
            return {"total_count": 1, "slow_sql_details": [{"sql_id": "sql-1", "node_id": node,
                "sql_text": "SELECT 1", "total_time": 1500000, "cpu_time": 0, "plan_time": 25,
                "io_time": None, "lock_time": 0}]}
        if operation == "real-time-session":
            return {"total_count": 999, "sessions": [{"session_id": f"session-{pid}", "pid": 0,
                "database_name": "synthetic-db", "query": "SELECT 1", "exec_time": 0}]}
        if operation == "records":
            return {"total_count": 2, "records": [
                {"record_id": "foreign-record", "instance_id": "another-instance", "status": "completed"},
                {"record_id": "record-own", "instance_id": CLOUD_ID, "status": "completed", "task_name": "原生巡检",
                 "start_time": START_MS, "end_time": END_MS, "normal_count": 0, "total_count": 2},
            ]}
        if operation == "record-own":
            return {"instance_id": CLOUD_ID, "start_time": START_MS, "end_time": END_MS, "inspection_results": [
                {"name": "CPU", "item": "cpu", "status": "normal", "indicators": [{"name": "CPU 使用率", "unit": "%",
                 "metrics": [{"name": node, "ip": "192.0.2.11", "min": 0, "max": 75.5, "average": 37.75,
                              "data": [{"time": [START_MS, START_MS + 60000, START_MS + 120000],
                                        "metrics": [0, 75.5, None]}]}]}]},
                {"name": "内存", "item": "memory", "status": "normal", "indicators": [{"name": "内存使用率", "unit": "%",
                 "metrics": [{"name": node, "data": {"time": [START_MS, START_MS + 60000], "metrics": [23]}}]}]},
            ]}
        pytest.fail(f"没有允许的合成云响应：{operation}")

    def send(self, request):
        self.requests.append(request)
        assert request.url.host and request.url.host.endswith(".invalid")
        assert request.url.path.startswith("/gaussdb/v3/project-")
        operation = request.url.path.rsplit("/", 1)[-1]
        query_posts = {"slow-sql-execute-node", "slow-sql-list", "slow-sql-detail", "real-time-session", "records"}
        assert request.method == ("POST" if operation in query_posts else "GET")
        pid = int(request.url.path.split("/")[3].removeprefix("project-"))
        value = self.overrides.get((pid, operation), self.overrides.get(operation))
        if value is None:
            value = self.default(operation, pid)
        if callable(value):
            value = value(request)
        status, body = value if isinstance(value, tuple) else (200, value)
        return httpx.Response(status, json=copy.deepcopy(body), request=request)


@pytest.fixture()
def gauss_api(mem_db, monkeypatch):
    settings = get_settings()
    for key, value in {"redis_url": None, "hcs_mock_url": None, "hcs_endpoints": {}, "hcs_ces_enabled": False,
                       "hcs_total_qps": 100000, "timezone": "UTC"}.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(service, "_cache", QueryCache())
    monkeypatch.setattr(ratelimit, "_shared", {})
    for tid in (1, 2):
        mem_db.add(HcsAccount(id=tid, code=f"gauss-isolated-{tid}", name=f"租户{tid}", enabled=True,
            iam_endpoint="https://iam.invalid", domain_name=f"domain-{tid}", username="reader",
            auth_type="AKSK", access_key="synthetic-access-key", secret_cipher="synthetic-cipher",
            endpoints={"gaussdb": f"https://gauss-{tid}.invalid"}, metrics_source="AOM"))
    mem_db.flush()
    for pid, tid in ((11, 1), (12, 1), (21, 2)):
        mem_db.add(HcsProject(id=pid, account_id=tid, project_id=f"project-{pid}", region_id="region-test",
                             external_domain="example.invalid", project_name=f"项目{pid}", enabled=True))
    mem_db.flush()
    for rid, tid, pid in ((111, 1, 11), (112, 1, 12), (211, 2, 21)):
        mem_db.add(Resource(id=rid, tenant_id=tid, project_ref=pid, service="gaussdb", resource_type="gaussdb_instance",
            resource_id=CLOUD_ID, name=f"实例{pid}", lifecycle="ACTIVE", status_raw="ACTIVE", health="OK", vcpus=8,
            attrs={"memory_size": 32, "memory_unit_verified": True, "volume_size": 100, "volume_unit_verified": True},
            metrics={"cpu_usage": 99, "mem_usage": 98, "disk_usage": 97}))
    mem_db.commit()
    cloud = NativeCloud()

    def no_network(*_args, **_kwargs):
        pytest.fail("测试禁止真实网络或默认云客户端")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    monkeypatch.setattr(service, "client_for", no_network)

    @contextmanager
    def mock_native(_db, target):
        tenant, project = target.tenant, target.project
        cloud.contexts.append((tenant.id, project.id, target.resource.id))
        templates = {**settings.hcs_endpoints, **tenant.endpoints}
        with httpx.Client(transport=httpx.MockTransport(cloud.send)) as http:
            yield HcsClient(http, AkSkAuth("synthetic-access", "synthetic-secret"),
                EndpointResolver(templates, project.region_id, project.external_domain), project.project_id,
                qps={"gaussdb": 100000}, max_retries=0, tenant_id=tenant.id, project_ref=project.id)

    monkeypatch.setattr(service, "native_client", mock_native)
    user = CurrentUser(id=901, username="gauss-reader", display_name="独立回归", permissions=list(PERMS), tenant_ids=[1])
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, user=user, cloud=cloud, db=mem_db, app=app)


@pytest.mark.parametrize("suffix,params", endpoint_cases())
@pytest.mark.parametrize("missing", ["ledger:view", "task:run"])
def test_permissions_require_view_and_native_diagnose_but_local_list_is_view_only(gauss_api, suffix, params, missing):
    g = gauss_api
    g.user.permissions = [permission for permission in PERMS if permission != missing]
    response = g.client.get(path(suffix), params=params)
    # 规范特例：本地台账列表只需 ledger:view；实际原生诊断再要求 task:run。
    expected = 200 if not suffix and missing == "task:run" else 403
    assert response.status_code == expected, response.text
    assert g.cloud.requests == [] and g.cloud.contexts == []


@pytest.mark.parametrize("suffix,params", [case for case in endpoint_cases() if case[0] == "export"])
def test_every_export_also_requires_export_permission(gauss_api, suffix, params):
    g = gauss_api
    g.user.permissions = ["ledger:view", "task:run"]
    assert g.client.get(path(suffix), params=params).status_code == 403
    assert g.cloud.requests == [] and g.cloud.contexts == []


def test_local_list_filters_real_tenant_scope_project_search_and_pagination_without_cloud(gauss_api):
    g = gauss_api
    first = g.client.get(path(""), params={"page_size": 1}).json()["data"]
    assert first["total"] == 2 and [row["id"] for row in first["items"]] == [111]
    second = g.client.get(path(""), params={"page_size": 1, "page": 2}).json()["data"]
    assert [row["id"] for row in second["items"]] == [112]
    filtered = g.client.get(path(""), params={"project_ref": 12, "keyword": "实例12"}).json()["data"]
    assert filtered["total"] == 1 and filtered["items"][0]["id"] == 112
    assert filtered["items"][0]["memory_unit"] == "GB" and filtered["items"][0]["storage_unit"] == "GB"
    assert g.client.get(path(""), params={"project_ref": 21}).json()["data"]["items"] == []
    assert g.client.get(path(""), params={"tenant_id": 2}).status_code == 404
    assert g.cloud.requests == []


@pytest.mark.parametrize("suffix,params", [case for case in endpoint_cases() if case[0]])
def test_foreign_tenant_same_cloud_id_is_404_and_does_not_open_native_client(gauss_api, suffix, params):
    g = gauss_api
    response = g.client.get(path(suffix, 211), params=params)
    assert response.status_code == 404, response.text
    assert g.cloud.requests == [] and g.cloud.contexts == []


def test_header_scope_cannot_bypass_real_user_scope_or_reuse_warm_foreign_cache(gauss_api):
    g = gauss_api
    g.user.tenant_ids = None
    assert g.client.get(path("overview", 211), headers={"X-Tenant-Id": "2"}).status_code == 200
    count = len(g.cloud.requests)
    g.user.tenant_ids = [1]
    assert g.client.get(path("overview", 211)).status_code == 404
    assert g.client.get(path("overview"), headers={"X-Tenant-Id": "2"}).status_code == 403
    assert len(g.cloud.requests) == count
    g.user.tenant_ids = None
    assert g.client.get(path("overview", 211), headers={"X-Tenant-Id": "1"}).status_code == 404
    assert len(g.cloud.requests) == count


def test_same_cloud_id_in_two_projects_and_tenants_has_separate_native_requests_and_cache(gauss_api):
    g = gauss_api
    g.user.tenant_ids = None
    values = []
    for rid in (111, 112, 211):
        response = g.client.get(path("overview", rid))
        assert response.status_code == 200, response.text
        values.append(response.json()["data"]["disk"]["used_gb"])
    assert values == [25, 50, 75]
    assert g.cloud.contexts == [(1, 11, 111), (1, 12, 112), (2, 21, 211)]
    assert len(g.cloud.requests) == 6
    assert {r.url.path.split("/")[3] for r in g.cloud.requests} == {"project-11", "project-12", "project-21"}
    for rid in (111, 112, 211):
        assert g.client.get(path("overview", rid)).status_code == 200
    assert len(g.cloud.requests) == 6


@pytest.mark.parametrize("disabled", ["tenant", "project"])
@pytest.mark.parametrize("suffix,params", [case for case in endpoint_cases() if case[0] and case[0] != "export"])
def test_disabled_target_has_no_cloud_calls(gauss_api, disabled, suffix, params):
    g = gauss_api
    target = g.db.get(HcsAccount, 1) if disabled == "tenant" else g.db.get(HcsProject, 11)
    target.enabled = False
    g.db.commit()
    response = g.client.get(path(suffix), params=params)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["disk"]["status"] if suffix == "overview" else data["status"]) == "DISABLED"
    assert g.cloud.requests == [] and g.cloud.contexts == []


@pytest.mark.parametrize("used,total,expected,status", [(25.5, 100, 25.5, "AVAILABLE"), (0, 100, 0, "AVAILABLE"),
    (0, 0, None, "PARTIAL"), ("12.5", "50", 25, "AVAILABLE")])
def test_native_disk_gb_zero_and_no_fake_current_cpu_or_memory(gauss_api, used, total, expected, status):
    g = gauss_api
    g.cloud.overrides["volume-usage"] = {"used": used, "total": total}
    response = g.client.get(path("overview"))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["source"] == "GAUSSDB" and data["disk"]["status"] == status
    assert data["disk"]["used_gb"] == float(used) and data["disk"]["total_gb"] == float(total)
    assert data["disk"]["usage_percent"] == expected and data["disk"]["observed_at"]
    assert data["cpu_usage"] is None and data["memory_usage"] is None
    assert data["instance"]["memory_size"] == 32 and data["instance"]["memory_unit"] == "GB"


@pytest.mark.parametrize("body", [{}, {"used": None, "total": 100}, {"used": -1, "total": 100},
    {"used": True, "total": 100}, {"used": 1, "total": False}, {"used": 1, "total": -100},
    {"used": "NaN", "total": 100}, {"used": 1, "total": "Infinity"}, {"used": [], "total": 100}])
def test_invalid_native_disk_cannot_become_measurement(gauss_api, body):
    g = gauss_api
    g.cloud.overrides["volume-usage"] = body
    data = g.client.get(path("overview")).json()["data"]
    assert data["disk"]["status"] == "ERROR"
    assert all(data["disk"][key] is None for key in ("used_gb", "total_gb", "usage_percent", "observed_at"))
    assert data["components_status"] == "AVAILABLE"


def test_slow_sql_queries_only_documented_post_paths_milliseconds_and_microseconds(gauss_api):
    g = gauss_api
    response = g.client.get(path("slow-sql"), params=slow_params(threshold=3))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "AVAILABLE", data
    assert data["items"][0]["avg_exec_time"] == 1500000 and data["items"][0]["avg_cpu_time"] == 0
    assert data["units"] == {"avg_exec_time": "微秒", "avg_cpu_time": "微秒", "avg_io_time": "微秒"}
    requests = g.cloud.requests
    assert [r.method for r in requests] == ["POST", "POST"]
    assert [r.url.path.rsplit("/", 1)[-1] for r in requests] == ["slow-sql-execute-node", "slow-sql-list"]
    assert json.loads(requests[0].content) == {"action": "slow"}
    assert json.loads(requests[1].content) == {"instance_id": CLOUD_ID, "node_ids": ["node-11"],
        "start_time": START_MS, "end_time": END_MS, "threshold": 3}
    assert all(not r.url.query for r in requests)


def test_slow_detail_uses_own_template_without_threshold_or_pagination_and_annotates_real_units(gauss_api):
    g = gauss_api
    response = g.client.get(path("slow-sql/detail"), params=slow_params(sql_id="sql-1"))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "AVAILABLE", data
    # gaussdb-api.pdf §4.9.3，正文 391–395 页：详情字段是 total_time，不是旧实现的 duration_time。
    assert data["items"][0]["total_time"] == 1500000 and data["items"][0]["cpu_time"] == 0
    assert data["items"][0]["plan_time"] == 25 and data["items"][0]["lock_time"] == 0
    assert json.loads(g.cloud.requests[-1].content) == {"instance_id": CLOUD_ID, "node_ids": ["node-11"],
        "start_time": START_MS, "end_time": END_MS, "sql_id": "sql-1"}
    assert data["units"] == {key: "微秒" for key in ("total_time", "cpu_time", "plan_time", "io_time", "lock_time")}


@pytest.mark.parametrize("suffix", ["slow-sql", "slow-sql/detail"])
def test_foreign_slow_node_rejected_before_querying_its_sql(gauss_api, suffix):
    g = gauss_api
    response = g.client.get(path(suffix), params=slow_params(node_ids=["node-21"], sql_id="sql-1"))
    assert response.status_code == 400, response.text
    assert [r.url.path.rsplit("/", 1)[-1] for r in g.cloud.requests] == ["slow-sql-execute-node"]


def test_slow_node_catalogue_with_foreign_instance_is_error_not_usable_nodes(gauss_api):
    g = gauss_api
    g.cloud.overrides["slow-sql-execute-node"] = {"nodes": [
        {"node_id": "node-11", "instance_id": "foreign-instance", "component_type": "DN"}]}
    data = g.client.get(path("slow-nodes")).json()["data"]
    assert data["status"] == "ERROR" and data["items"] == []


@pytest.mark.parametrize("wrong", [{"node_id": "node-21", "sql_id": "sql-1"},
                                  {"node_id": "node-11", "sql_id": "foreign-sql"}])
def test_slow_detail_rejects_mismatched_node_or_sql_in_upstream_response(gauss_api, wrong):
    g = gauss_api
    g.cloud.overrides["slow-sql-detail"] = {"slow_sql_details": [wrong]}
    data = g.client.get(path("slow-sql/detail"), params=slow_params(sql_id="sql-1")).json()["data"]
    assert data["status"] == "ERROR" and data["items"] == []


@pytest.mark.parametrize("limit", [1, 200])
def test_sessions_bounded_read_only_query_does_not_claim_upstream_total(gauss_api, limit):
    g = gauss_api
    params = {"node_id": "node-11", "component_id": "component-11", "limit": limit,
              "database_name": "test-db", "user_name": "reader", "wait": "f"}
    response = g.client.get(path("sessions"), params=params)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == ("PARTIAL" if limit == 1 else "AVAILABLE"), data
    assert data["total"] is None and data["items"][0]["exec_time"] == 0
    assert data["truncated"] is (limit == 1)
    assert data["status"] == ("PARTIAL" if limit == 1 else "AVAILABLE")
    assert json.loads(g.cloud.requests[-1].content) == {"node_id": "node-11", "component_id": "component-11",
        "limit": limit, "is_statistic_open": "false",
        "query_info": {"database_name": "test-db", "user_name": "reader", "wait": "f"}}


@pytest.mark.parametrize("limit", [0, 201, -1])
def test_invalid_session_limits_rejected_before_any_cloud_call(gauss_api, limit):
    g = gauss_api
    response = g.client.get(path("sessions"), params={"node_id": "node-11", "component_id": "component-11", "limit": limit})
    assert response.status_code == 400 and g.cloud.requests == []


@pytest.mark.parametrize("node,component", [("node-21", "component-21"), ("node-11", "component-21")])
def test_sessions_require_node_and_component_pair_from_this_instance(gauss_api, node, component):
    g = gauss_api
    response = g.client.get(path("sessions"), params={"node_id": node, "component_id": component})
    assert response.status_code == 400, response.text
    assert [r.url.path.rsplit("/", 1)[-1] for r in g.cloud.requests] == ["components"]


def test_inspections_filter_foreign_instances_and_keep_native_millisecond_window(gauss_api):
    g = gauss_api
    response = g.client.get(path("inspections"), params={"start_at": START.isoformat(), "end_at": END.isoformat()})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 1 and [row["record_id"] for row in data["items"]] == ["record-own"]
    request = g.cloud.requests[0]
    assert request.method == "POST" and request.url.path == "/gaussdb/v3/project-11/inspections/records"
    assert json.loads(request.content)["inspection_start_time_range"] == {"start_time": START_MS, "end_time": END_MS}
    assert data["items"][0]["normal_count"] == 0


def test_inspection_detail_checks_instance_owner_before_return_or_export(gauss_api):
    g = gauss_api
    g.cloud.overrides["foreign-record"] = {"instance_id": "foreign-instance", "inspection_results": [
        {"name": "PRIVATE_FOREIGN_REPORT", "indicators": []}]}
    response = g.client.get(path("inspections/foreign-record"))
    assert response.status_code == 404 and "PRIVATE_FOREIGN_REPORT" not in response.text
    assert len(g.cloud.requests) == 1
    exported = g.client.get(path("export"), params={"panel": "inspection-detail", "record_id": "foreign-record"})
    assert exported.status_code == 404 and "PRIVATE_FOREIGN_REPORT" not in exported.text
    assert all(r.method == "GET" and r.url.path.endswith("/inspections/records/foreign-record") for r in g.cloud.requests)


@pytest.mark.parametrize("suffix,params", [case for case in endpoint_cases() if case[0] and case[0] != "export"])
def test_same_query_and_corresponding_export_reuse_cached_native_result(gauss_api, suffix, params):
    g = gauss_api
    response = g.client.get(path(suffix), params=params)
    assert response.status_code == 200, response.text
    before = len(g.cloud.requests)
    assert before > 0
    assert g.client.get(path(suffix), params=params).json() == response.json()
    assert len(g.cloud.requests) == before
    if suffix in ("slow-nodes", "slow-sql/detail"):
        return  # 这两个只读入口尚无对应导出面板；不把其他面板冒充同查询。
    panel = "inspection-detail" if suffix.startswith("inspections/") else suffix
    export_params = {"panel": panel, **params}
    if panel == "inspection-detail":
        export_params["record_id"] = "record-own"
    if panel == "inspections":
        export_params["limit"] = 50
    exported = g.client.get(path("export"), params=export_params)
    assert exported.status_code == 200, exported.text if exported.status_code != 200 else ""
    assert exported.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert len(g.cloud.requests) == before


@pytest.mark.parametrize("field", ["endpoint", "iam_endpoint", "domain_name", "domain_id", "username", "access_key",
                                   "secret_cipher", "auth_type", "project_id", "external_domain", "global_endpoint"])
def test_endpoint_and_account_identity_change_never_reuses_prior_cache(gauss_api, field, monkeypatch):
    g = gauss_api
    assert g.client.get(path("overview")).json()["data"]["disk"]["used_gb"] == 25
    tenant, project = g.db.get(HcsAccount, 1), g.db.get(HcsProject, 11)
    if field == "endpoint":
        tenant.endpoints = {"gaussdb": "https://changed-gauss.invalid"}
    elif field == "global_endpoint":
        monkeypatch.setattr(get_settings(), "hcs_endpoints", {"gaussdb": "https://global-changed.invalid"})
    elif field == "project_id":
        project.project_id = "project-31"
    elif field == "external_domain":
        project.external_domain = "changed.invalid"
    else:
        setattr(tenant, field, "PASSWORD" if field == "auth_type" else "synthetic-changed")
    g.db.commit()
    g.cloud.overrides["volume-usage"] = {"used": 61, "total": 100}
    assert g.client.get(path("overview")).json()["data"]["disk"]["used_gb"] == 61
    assert len(g.cloud.requests) == 4 and len(g.cloud.contexts) == 2


def test_report_keeps_actual_cpu_memory_points_without_relabeling_them_as_current(gauss_api):
    g = gauss_api
    data = g.client.get(path("inspections/record-own")).json()["data"]
    assert data["start_at"] == START.isoformat().replace("+00:00", "Z")
    assert data["end_at"] == END.isoformat().replace("+00:00", "Z")
    cpu = data["items"][0]["indicators"][0]
    assert cpu["unit"] == "%"
    assert cpu["metrics"][0]["data"][0] == {
        "time": [START_MS, START_MS + 60000, START_MS + 120000], "metrics": [0, 75.5, None]}
    assert data["items"][1]["indicators"][0]["metrics"][0]["data"]["metrics"] == [23]
    overview = g.client.get(path("overview")).json()["data"]
    assert overview["cpu_usage"] is None and overview["memory_usage"] is None


def workbook(response):
    assert response.status_code == 200, response.text if response.status_code != 200 else ""
    assert response.content.startswith(b"PK")
    return load_workbook(io.BytesIO(response.content), data_only=False)


def assert_no_formulas(book):
    assert all(cell.data_type != "f" for sheet in book for row in sheet for cell in row)


def test_xlsx_slow_sql_formula_safety_numbers_zeros_and_missing_values(gauss_api):
    g = gauss_api
    g.db.get(Resource, 111).name = '=HYPERLINK("https://example.invalid","name")'
    g.db.get(HcsAccount, 1).name = "+tenant"
    g.db.commit()
    body = g.cloud.default("slow-sql-list", 11)
    body["slow_sql_infos"][0].update(sql_text="=1+1", user_name="@cmd", calls="0", avg_cpu_time=0,
                                     avg_io_time=None, query_plan="-formula", buffer_hit_ratio="invalid")
    g.cloud.overrides["slow-sql-list"] = body
    response = g.client.get(path("export"), params={"panel": "slow-sql", **slow_params()})
    book = workbook(response)
    assert_no_formulas(book)
    sheet = book["慢SQL"]
    assert sheet["E2"].value == "=1+1" and sheet["E2"].data_type == "s"
    assert sheet["G2"].value == 0 and sheet["G2"].data_type == "n"
    assert sheet["H2"].value == 1500000 and sheet["I2"].value == 0
    assert sheet["J2"].value is None and sheet["M2"].value is None
    assert book["查询条件"]["B2"].value == "+tenant"


def test_xlsx_report_preserves_real_times_and_missing_points_and_never_fills_with_zero(gauss_api):
    g = gauss_api
    response = g.client.get(path("export"), params={"panel": "inspection-detail", "record_id": "record-own"})
    book = workbook(response)
    assert_no_formulas(book)
    rows = list(book["巡检指标"].iter_rows(min_row=2, values_only=True))
    assert len(rows) == 5
    assert [row[8] for row in rows] == [0, 75.5, None, 23, None]
    assert [row[7] for row in rows[:3]] == [
        START.replace(tzinfo=None), (START + timedelta(minutes=1)).replace(tzinfo=None),
        (START + timedelta(minutes=2)).replace(tzinfo=None)]
    assert all(row[4] == "%" for row in rows)


@pytest.mark.parametrize("status", [401, 403, 404, 405, 501, 503])
def test_upstream_errors_do_not_leak_response_details_or_fabricate_measurements(gauss_api, status):
    g = gauss_api
    g.cloud.overrides["slow-sql-execute-node"] = (status, {"error_code": "SYNTHETIC.ERROR", "message": "PRIVATE_UPSTREAM_TEXT"})
    response = g.client.get(path("slow-nodes"))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == ("UNAVAILABLE" if status != 503 else "ERROR") and data["items"] == []
    assert "PRIVATE_UPSTREAM_TEXT" not in response.text and "synthetic-secret" not in response.text


@pytest.mark.parametrize("suffix,params", [case for case in endpoint_cases() if case[0] == "export"])
@pytest.mark.parametrize("disabled", ["tenant", "project"])
def test_disabled_exports_do_not_call_native_cloud(gauss_api, suffix, params, disabled):
    g = gauss_api
    target = g.db.get(HcsAccount, 1) if disabled == "tenant" else g.db.get(HcsProject, 11)
    target.enabled = False
    g.db.commit()
    book = workbook(g.client.get(path(suffix), params=params))
    assert_no_formulas(book)
    assert "DISABLED" in [cell.value for sheet in book for row in sheet for cell in row]
    assert g.cloud.requests == [] and g.cloud.contexts == []


@pytest.mark.parametrize("disabled", ["tenant", "project"])
def test_disabling_target_precedes_previously_successful_overview_cache(gauss_api, disabled):
    g = gauss_api
    assert g.client.get(path("overview")).json()["data"]["disk"]["status"] == "AVAILABLE"
    count = len(g.cloud.requests)
    target = g.db.get(HcsAccount, 1) if disabled == "tenant" else g.db.get(HcsProject, 11)
    target.enabled = False
    g.db.commit()
    data = g.client.get(path("overview")).json()["data"]
    assert data["disk"]["status"] == "DISABLED" and data["disk"]["used_gb"] is None
    assert len(g.cloud.requests) == count


@pytest.mark.parametrize("change", ["deleted", "wrong_type"])
def test_non_active_or_other_product_resource_is_404_before_cloud(gauss_api, change):
    g = gauss_api
    row = g.db.get(Resource, 111)
    if change == "deleted":
        row.lifecycle = "DELETED"
    else:
        row.resource_type, row.service = "dcs_instance", "dcs"
    g.db.commit()
    for suffix, params in endpoint_cases():
        if suffix:
            assert g.client.get(path(suffix), params=params).status_code == 404
    assert g.cloud.requests == [] and g.cloud.contexts == []


@pytest.mark.parametrize("suffix", ["slow-sql", "slow-sql/detail", "inspections"])
@pytest.mark.parametrize("start,end", [(START.replace(tzinfo=None), END), (END, START), (START, START),
                                     (START, START + timedelta(days=31, seconds=1))])
def test_invalid_windows_rejected_without_any_cloud_query(gauss_api, suffix, start, end):
    g = gauss_api
    params = {"start_at": start.isoformat(), "end_at": end.isoformat()}
    if suffix.startswith("slow-sql"):
        params.update(node_ids=["node-11"], sql_id="sql-1")
    response = g.client.get(path(suffix), params=params)
    assert response.status_code == 400, response.text
    assert g.cloud.requests == [] and g.cloud.contexts == []


@pytest.mark.parametrize("suffix,params", [
    ("sessions", {"node_id": "../foreign", "component_id": "component-11"}),
    ("sessions", {"node_id": "node-11", "component_id": "component-11", "wait": "yes"}),
    ("slow-sql", slow_params(node_ids=["../foreign"])),
    ("slow-sql", slow_params(threshold=0)), ("slow-sql", slow_params(threshold=100)),
    ("slow-sql/detail", slow_params()),
    ("export", {"panel": "slow-sql"}), ("export", {"panel": "sessions"}),
    ("export", {"panel": "inspection-detail"}),
])
def test_invalid_identifiers_filters_or_missing_export_conditions_never_query_cloud(gauss_api, suffix, params):
    g = gauss_api
    response = g.client.get(path(suffix), params=params)
    assert response.status_code == 400, response.text
    assert g.cloud.requests == [] and g.cloud.contexts == []


@pytest.mark.parametrize("detail", [False, True])
@pytest.mark.parametrize("declared_total,expected_status", [(7, "PARTIAL"), (1, "AVAILABLE"), (None, "AVAILABLE")])
def test_slow_sql_retains_only_native_batch_and_marks_known_truncation_without_inventing_pagination(
        gauss_api, detail, declared_total, expected_status):
    g = gauss_api
    operation = "slow-sql-detail" if detail else "slow-sql-list"
    body = g.cloud.default(operation, 11)
    if declared_total is None:
        body.pop("total_count")
    else:
        body["total_count"] = declared_total
    g.cloud.overrides[operation] = body
    response = g.client.get(path("slow-sql/detail" if detail else "slow-sql"), params=slow_params(sql_id="sql-1"))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data["items"]) == 1 and data["total"] == declared_total
    assert data["status"] == expected_status and data["truncated"] is (declared_total == 7)
    if declared_total == 7:
        assert data["warning"]
    requests = [request for request in g.cloud.requests if request.url.path.endswith("/" + operation)]
    assert len(requests) == 1
    assert not ({"limit", "offset", "page", "page_size", "marker", "next_marker"} & json.loads(requests[0].content).keys())


@pytest.mark.parametrize("limit,returned", [(2, 2), (2, 3), (200, 200)])
def test_full_session_batch_always_warns_possible_truncation_and_no_total(gauss_api, limit, returned):
    g = gauss_api
    g.cloud.overrides["real-time-session"] = {"total_count": returned, "sessions": [
        {"session_id": f"session-{i}", "exec_time": i} for i in range(returned)]}
    data = g.client.get(path("sessions"), params={"node_id": "node-11", "component_id": "component-11",
                                               "limit": limit}).json()["data"]
    assert data["status"] == "PARTIAL" and data["truncated"] and data["warning"]
    assert data["total"] is None and len(data["items"]) == limit
    assert [row["session_id"] for row in data["items"]] == [f"session-{i}" for i in range(limit)]
    posts = [request for request in g.cloud.requests if request.method == "POST"]
    assert len(posts) == 1 and json.loads(posts[0].content)["is_statistic_open"] == "false"


def test_inspection_local_filter_rechecks_owner_completed_and_half_open_time_before_pagination(gauss_api):
    g = gauss_api
    def record(rid, **extra):
        return {"record_id": rid, "instance_id": CLOUD_ID, "status": "completed", "start_time": START_MS, **extra}
    rows = [
        record("foreign", instance_id="other-instance"), record("running", status="running"),
        record("unknown-status", status=None), record("too-old", start_time=START_MS - 1),
        record("at-exclusive-end", start_time=END_MS), record("too-new", start_time=END_MS + 1),
        record("missing-time", start_time=None), record("bad-time", start_time=True),
        record("inside-last", start_time=END_MS - 1), record("at-inclusive-start", start_time=START_MS),
    ]
    g.cloud.overrides["records"] = {"records": rows, "total_count": len(rows)}
    params = {"start_at": START.isoformat(), "end_at": END.isoformat(), "limit": 1, "offset": 1}
    data = g.client.get(path("inspections"), params=params).json()["data"]
    assert data["status"] == "AVAILABLE" and data["total"] == 2
    assert [row["record_id"] for row in data["items"]] == ["at-inclusive-start"]
    assert not data["truncated"]
    request = g.cloud.requests[0]
    body = json.loads(request.content)
    assert body["status"] == "completed" and body["inspection_start_time_range"] == {
        "start_time": START_MS, "end_time": END_MS}
    before = len(g.cloud.requests)
    exported = workbook(g.client.get(path("export"), params={"panel": "inspections", **params}))
    assert len(g.cloud.requests) == before
    assert exported["巡检记录"]["A2"].value == "at-inclusive-start"


def test_inspection_report_retains_historical_points_outside_task_check_window_in_api_and_xlsx(gauss_api):
    g = gauss_api
    historical = START - timedelta(days=3)
    old_ms = int(historical.timestamp() * 1000)
    report = g.cloud.default("record-own", 11)
    cpu = report["inspection_results"][0]["indicators"][0]
    cpu["metrics"][0]["data"][0] = {"time": [old_ms, old_ms + 60000, old_ms + 120000], "metrics": [0, 75.5, None]}
    memory = report["inspection_results"][1]["indicators"][0]
    memory.update(unit="MB")
    memory["metrics"][0]["data"] = {"time": [old_ms, old_ms + 60000], "metrics": [2048]}
    g.cloud.overrides["record-own"] = report
    response = g.client.get(path("inspections/record-own"))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["start_at"] == START.isoformat().replace("+00:00", "Z")
    assert data["end_at"] == END.isoformat().replace("+00:00", "Z")
    assert data["items"][0]["indicators"][0]["metrics"][0]["data"][0]["time"] == [old_ms, old_ms + 60000, old_ms + 120000]
    before = len(g.cloud.requests)
    book = workbook(g.client.get(path("export"), params={"panel": "inspection-detail", "record_id": "record-own"}))
    assert len(g.cloud.requests) == before
    rows = list(book["巡检指标"].iter_rows(min_row=2, values_only=True))
    assert [row[8] for row in rows] == [0, 75.5, None, 2048, None]
    assert [row[4] for row in rows] == ["%", "%", "%", "MB", "MB"]
    assert rows[0][7] == historical.replace(tzinfo=None)
    assert rows[2][7] == (historical + timedelta(minutes=2)).replace(tzinfo=None)
    assert all(row[7] < START.replace(tzinfo=None) for row in rows)
    conditions = {row[0].value: row[1].value for row in book["查询条件"].iter_rows(min_row=2)}
    assert conditions["巡检开始时间"] == START.replace(tzinfo=None)
    assert conditions["巡检结束时间"] == END.replace(tzinfo=None)
    assert_no_formulas(book)


def test_overview_query_and_export_share_thirty_second_cache_without_sleep(gauss_api, monkeypatch):
    g = gauss_api
    clock = {"seconds": 100.0}
    monkeypatch.setattr(query_cache, "time", SimpleNamespace(monotonic=lambda: clock["seconds"]))
    assert g.client.get(path("overview")).json()["data"]["disk"]["used_gb"] == 25
    assert len(g.cloud.requests) == 2
    g.cloud.overrides["volume-usage"] = {"used": 66, "total": 100}
    clock["seconds"] = 129.9
    book = workbook(g.client.get(path("export"), params={"panel": "overview"}))
    assert book["磁盘与配置"]["B2"].value == 25 and len(g.cloud.requests) == 2
    clock["seconds"] = 130.1
    assert g.client.get(path("overview")).json()["data"]["disk"]["used_gb"] == 66
    assert len(g.cloud.requests) == 4


@pytest.mark.parametrize("operation", ["slow-sql-execute-node", "slow-sql-list", "slow-sql-detail",
                                        "real-time-session", "records"])
def test_real_cloud_client_allows_exact_documented_native_query_posts(gauss_api, operation):
    g = gauss_api
    target = service.Target(g.db.get(Resource, 111), g.db.get(HcsAccount, 1), g.db.get(HcsProject, 11))
    native_path = ("/gaussdb/v3/{project_id}/inspections/records" if operation == "records" else
                   "/gaussdb/v3/{project_id}/instances/" + CLOUD_ID + "/" + operation)
    payloads = {
        "slow-sql-execute-node": {"action": "slow"},
        "slow-sql-list": {"instance_id": CLOUD_ID, "node_ids": ["node-11"],
                          "start_time": START_MS, "end_time": END_MS, "threshold": 1},
        "slow-sql-detail": {"instance_id": CLOUD_ID, "node_ids": ["node-11"],
                            "start_time": START_MS, "end_time": END_MS, "sql_id": "sql-1"},
        "real-time-session": {"node_id": "node-11", "component_id": "component-11", "limit": 1,
                              "is_statistic_open": "false"},
        "records": {"instance_name": "实例11", "status": "completed", "limit": 100, "offset": 0},
    }
    with service.native_client(g.db, target) as client:
        data = client.post("gaussdb", native_path, json=payloads[operation])
    assert isinstance(data, dict) and len(g.cloud.requests) == 1
    assert g.cloud.requests[0].method == "POST"


@pytest.mark.parametrize("method,suffix", [("POST", "volume-usage"), ("POST", "components"),
    ("POST", "arbitrary-write"), ("POST", "real-time-session/terminate"),
    ("PUT", "slow-sql-list"), ("PATCH", "real-time-session"), ("DELETE", "slow-sql-detail")])
def test_real_cloud_client_never_accepts_mutations_or_undocumented_posts(gauss_api, method, suffix):
    g = gauss_api
    target = service.Target(g.db.get(Resource, 111), g.db.get(HcsAccount, 1), g.db.get(HcsProject, 11))
    with service.native_client(g.db, target) as client, pytest.raises(HcsError) as raised:
        client.request("gaussdb", method, "/gaussdb/v3/{project_id}/instances/" + CLOUD_ID + "/" + suffix, json={})
    assert raised.value.code == "LOCAL.READ_ONLY"
    assert g.cloud.requests == []


def test_xlsx_report_external_formula_text_remains_literal_without_extra_cloud_query(gauss_api):
    g = gauss_api
    report = g.cloud.default("record-own", 11)
    report["inspection_results"][0]["name"] = "=1+1"
    report["inspection_results"][0]["indicators"][0]["name"] = "+formula-indicator"
    report["inspection_results"][0]["indicators"][0]["metrics"][0]["name"] = "@formula-node"
    g.cloud.overrides["record-own"] = report
    assert g.client.get(path("inspections/record-own")).status_code == 200
    before = len(g.cloud.requests)
    book = workbook(g.client.get(path("export"), params={"panel": "inspection-detail", "record_id": "record-own"}))
    assert len(g.cloud.requests) == before
    assert_no_formulas(book)
    assert book["巡检结果"]["A2"].value == "=1+1" and book["巡检结果"]["A2"].data_type == "s"
    points = book["巡检指标"]
    assert points["A2"].value == "=1+1" and points["D2"].value == "+formula-indicator"
    assert points["F2"].value == "@formula-node"
    assert points["I2"].value == 0 and points["I2"].data_type == "n"
    assert points["I4"].value is None


def test_xlsx_overview_gb_numbers_zeros_and_current_nulls_match_cached_api(gauss_api):
    g = gauss_api
    g.cloud.overrides["volume-usage"] = {"used": 0, "total": 100}
    assert g.client.get(path("overview")).json()["data"]["disk"]["usage_percent"] == 0
    before = len(g.cloud.requests)
    book = workbook(g.client.get(path("export"), params={"panel": "overview"}))
    assert len(g.cloud.requests) == before
    disk = book["磁盘与配置"]
    assert disk["B2"].value == 0 and disk["B2"].data_type == "n"
    assert disk["B3"].value == 100 and disk["B4"].value == 0
    assert disk["C2"].value == disk["C3"].value == "GB"
    assert disk["B7"].value is None and disk["B8"].value is None
    assert_no_formulas(book)
