"""Global AOM shutdown regression: isolated DB, synthetic identity and no real network."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select

from app.api.v1.resources import router as resources_router
from app.collectors.base import RunContext
from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_current_user
from app.hcs import HcsClient, HcsError
from app.hcs import client as cloud_client
from app.hcs.ratelimit import consume_budget, request_budget
from app.ledger.excel import COMMON, export_ledger
from app.main import app as production_app
from app.metrics import (
    aom,
    aom_configured,
    available,
    ces,
    effective,
    guarded_aom,
    mark,
    mode,
    use_aom,
    use_prometheus,
)
from app.models import HcsAccount, HcsProject, Resource
from app.reports import builder
from app.services import dashboard_service, dcs_history_service, dcs_monitoring, ecs_monitoring, metric_visibility
from app.services.catalog import RESOURCE_TYPES
from app.services.metric_visibility import current_metrics

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def forbidden(*_args, **_kwargs):
    pytest.fail("AOM reached endpoint resolution, auth, cache, network, or request-budget consumption")


@pytest.mark.parametrize("service", ["aom", "AOM", " aOm "])
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "get", ""])
@pytest.mark.parametrize("path", [
    "/v1/{project_id}/ams/metrics", "/v1/{project_id}/ams/metricdata", "/v2/{project_id}/samples",
    "/v2/{project_id}/series", "/unknown/read", "//invalid/path?with=query#fragment",
])
def test_cloud_client_blocks_all_aom_methods_and_paths_before_endpoint_auth_or_network(
        monkeypatch, service, method, path):
    monkeypatch.setattr(get_settings(), "redis_url", None)
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as http:
        auth = SimpleNamespace(apply=forbidden, refresh=forbidden)
        resolver = SimpleNamespace(url=forbidden, base=forbidden)
        client = HcsClient(http, auth, resolver, "synthetic-project", max_retries=3,
                           tenant_id=1, project_ref=11)
        for name in ("consume_budget", "service_limiter", "acquire_cloud"):
            monkeypatch.setattr(cloud_client, name, forbidden)
        monkeypatch.setattr(http, "build_request", forbidden)
        with request_budget(attempts=1, seconds=5):
            with pytest.raises(HcsError) as error:
                client._send(service, method, path, json={"old_mapping": True},
                             params={"limit": 1}, headers={"X-Synthetic": "test"})
            assert error.value.code == "LOCAL.AOM_DISABLED" and error.value.status == 0
            consume_budget()  # The local guard did not consume the outer request budget.
        assert client.calls == 0 and client.metrics_snapshot()["services"] == {}


@pytest.mark.parametrize("configured_mode", ["AOM", "aom", "AUTO", "PROMETHEUS", "NONE", "CES"])
@pytest.mark.parametrize("prometheus_url", [None, "", "https://prom.invalid"])
@pytest.mark.parametrize("capability", [None, False, True])
def test_source_selection_never_reactivates_aom_or_implicitly_falls_back(
        monkeypatch, configured_mode, prometheus_url, capability):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", False)
    caps = {} if capability is None else {"aom": {"available": capability, "reason": "legacy evidence"}}
    tenant = SimpleNamespace(metrics_source=configured_mode, prometheus_url=prometheus_url,
                             endpoints={"aom": "https://aom.invalid"}, capabilities=deepcopy(caps))
    before = deepcopy(tenant.__dict__)
    expected = "PROMETHEUS" if configured_mode in ("AUTO", "PROMETHEUS") and prometheus_url else "NONE"
    assert mode(tenant) == configured_mode.upper()  # Old AOM mode is readable, not rewritten.
    assert effective(tenant) == expected
    assert available(tenant, "aom") is False
    assert use_aom(tenant) is False and aom_configured(tenant) is False
    assert use_prometheus(tenant) is (expected == "PROMETHEUS")
    mark(tenant, "aom", True, "must not be written", project_ref=11)
    mark(tenant, "aom", False, "must not be written", project_ref=12)
    assert tenant.__dict__ == before


def test_legacy_guarded_aom_never_calls_callback_or_marks_capabilities():
    tenant = SimpleNamespace(metrics_source="AOM", capabilities={"aom": {"available": True}})
    context = RunContext(client=SimpleNamespace(), account="synthetic", project_id="p1", tenant=tenant)
    empty = {"sentinel": "returned unchanged"}
    assert guarded_aom(context, forbidden, empty) is empty
    assert context.errors == [] and tenant.capabilities == {"aom": {"available": True}}
    assert not hasattr(aom, "_cache") and not hasattr(aom, "metric_key")


@pytest.mark.parametrize("gate,configured_mode,expected", [
    (False, "CES", "NONE"), (True, "CES", "CES"), (True, "AUTO", "NONE"), (True, "AOM", "NONE"),
])
def test_legacy_ces_remains_a_separate_double_opt_in(monkeypatch, gate, configured_mode, expected):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", gate)
    tenant = SimpleNamespace(metrics_source=configured_mode, prometheus_url=None,
                             endpoints={"aom": "https://aom.invalid", "ces": "https://ces.invalid"})
    assert effective(tenant) == expected


def ces_descriptor():
    return {"namespace": "AGT.ECS", "metric_name": "disk_usedPercent", "dimensions": [
        {"name": "mount_point", "value": "/"}, {"name": "instance_id", "value": "one"}]}


def test_ces_descriptor_identity_is_order_independent_and_does_not_use_aom(monkeypatch):
    for name in ("aom_query", "aom_list_metrics", "discover", "resolve_series"):
        monkeypatch.setattr(aom, name, forbidden)
    descriptor = ces_descriptor()
    reordered = {**descriptor, "dimensions": list(reversed(descriptor["dimensions"]))}
    assert ces.metric_key(descriptor) == ces.metric_key(reordered)
    for changed in ({**descriptor, "namespace": "OTHER"}, {**descriptor, "metric_name": "other"},
                    {**descriptor, "dimensions": [{"name": "instance_id", "value": "two"}]}):
        assert ces.metric_key(descriptor) != ces.metric_key(changed)
    assert descriptor == ces_descriptor()


@pytest.mark.parametrize("bad", [None, [], {}, {"namespace": "N", "metric_name": "cpu", "dimensions": []},
    {**ces_descriptor(), "namespace": ""}, {**ces_descriptor(), "metric_name": True},
    {**ces_descriptor(), "dimensions": [{"name": "instance_id", "value": True}]},
    {**ces_descriptor(), "dimensions": [{"name": "instance_id", "value": "one"}]*2},
])
def test_ces_descriptor_validation_retains_its_own_error_contract(bad):
    with pytest.raises(HcsError) as error:
        ces.metric_key(bad)
    assert error.value.code == "LOCAL.CES_RESPONSE"


@pytest.fixture(params=["metrics_source", "ecs_monitoring"])
def old_resources(mem_db, monkeypatch, request):
    for module in (ecs_monitoring, dashboard_service, builder, dcs_monitoring, metric_visibility):
        monkeypatch.setattr(module, "utcnow", lambda: NOW)
    for tid in (1, 2):
        mem_db.add(HcsAccount(id=tid, code=f"synthetic-{tid}", name=f"租户{tid}",
            iam_endpoint="https://iam.invalid", domain_name=f"synthetic-{tid}", enabled=True,
            metrics_source="AOM", prometheus_url="https://prom.invalid", endpoints={"aom": "https://aom.invalid"}))
    mem_db.flush()
    for tid, pid in ((1, 11), (2, 21)):
        mem_db.add(HcsProject(id=pid, account_id=tid, project_id=f"project-{pid}", project_name=f"项目{pid}",
            region_id="test", external_domain="invalid", enabled=True))
    mem_db.flush()
    old_attrs = {"metrics_source": "AOM"} if request.param == "metrics_source" else {"ecs_monitoring": {"source": "AOM"}}
    for rid, service_name, rtype, metrics in [
        (1, "ecs", "ecs_server", {"cpu_util": 99, "mem_util": 98, "disk_util": 97, "disk_mount": "/legacy"}),
        (2, "dcs", "dcs_instance", {"memory_usage": 96, "used_memory_mb": 49152, "max_memory_mb": 51200}),
        (3, "gaussdb", "gaussdb_instance", {"disk_usage": 94, "cpu_usage": 93, "mem_usage": 92}),
    ]:
        mem_db.add(Resource(id=rid, tenant_id=1, project_ref=11, service=service_name, resource_type=rtype,
            resource_id=f"cloud-{rid}", name=f"历史资源{rid}", ip="10.0.0.1", status_raw="ACTIVE",
            attrs=deepcopy(old_attrs), metrics=metrics, health="OK", monitored=True, last_seen=NOW))
    mem_db.add(Resource(id=4, tenant_id=1, project_ref=11, service="dcs", resource_type="dcs_instance",
        resource_id="native-dcs", name="原生缓存", status_raw="RUNNING", attrs={"metrics_source": "DCS",
            "dcs_monitoring": {"source": "DCS", "api_version": "v2", "endpoint": "instances/statistic",
                               "time_basis": "RESPONSE_RECEIVED", "observed_at": NOW.isoformat()}},
        metrics={"memory_usage": 0, "used_memory_mb": 0, "max_memory_mb": 1024}, health="OK", last_seen=NOW))
    mem_db.add(Resource(id=5, tenant_id=2, project_ref=21, service="gaussdb", resource_type="gaussdb_instance",
        resource_id="foreign", name="其他租户", attrs={"metrics_source": "GAUSSDB"}, metrics={"disk_usage": 77},
        status_raw="ACTIVE", last_seen=NOW))
    mem_db.commit()
    before = {row.id: (deepcopy(row.attrs), deepcopy(row.metrics), row.last_seen)
              for row in mem_db.scalars(select(Resource))}
    yield mem_db
    # Read/display/export filtering must never erase or rewrite the persisted raw source/value.
    mem_db.expire_all()
    assert {row.id: (row.attrs, row.metrics, row.last_seen)
            for row in mem_db.scalars(select(Resource))} == before
    assert not mem_db.dirty


def visibility_reader():
    user = CurrentUser(id=100, username="reader", display_name="读者", tenant_ids=[1],
                       permissions=["ledger:view", "ledger:export", "report:view"])
    return Scope([1], False, user)


def test_current_metrics_hides_old_aom_raw_values_without_mutation(old_resources):
    db = old_resources
    for rid in (1, 2, 3):
        resource = db.get(Resource, rid)
        original = deepcopy(resource.metrics)
        visible = current_metrics(resource)
        assert visible and all(value is None for value in visible.values())
        assert resource.metrics == original
    assert current_metrics(db.get(Resource, 4))["memory_usage"] == 0


def test_ecs_current_filter_keeps_native_reboot_count_while_hiding_disabled_usage():
    resource = SimpleNamespace(resource_type="ecs_server",
        attrs={"metrics_source": "NONE", "ecs_monitoring": {"source": "AOM"}},
        metrics={"cpu_util": 99, "mem_util": 98, "disk_util": 97, "disk_mount": "/", "reboots_24h": 2})
    before = deepcopy(resource.metrics)
    visible = current_metrics(resource)
    assert visible["reboots_24h"] == 2
    assert all(visible[key] is None for key in ("cpu_util", "mem_util", "disk_util", "disk_mount"))
    assert resource.metrics == before


def test_resource_table_and_detail_do_not_expose_aom_as_current(old_resources):
    app = FastAPI()
    app.include_router(resources_router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: old_resources
    app.dependency_overrides[get_current_user] = lambda: visibility_reader().user
    with TestClient(app) as client:
        response = client.get("/api/v1/resources")
        assert response.status_code == 200, response.text
        rows = {row["id"]: row for row in response.json()["data"]["items"]}
        assert set(rows) == {1, 2, 3, 4}
        for rid in (1, 2, 3):
            assert all(value is None for value in rows[rid]["metrics"].values())
            detail = client.get(f"/api/v1/resources/{rid}")
            assert detail.status_code == 200, detail.text
            assert all(value is None for value in detail.json()["data"]["resource"]["metrics"].values())
        assert rows[4]["metrics"]["memory_usage"] == 0
        assert client.get("/api/v1/resources/5").status_code == 404


def test_overview_and_report_capacity_exclude_legacy_aom_but_keep_native_zero(old_resources):
    data = dashboard_service.summary(old_resources, NOW, tenant_ids=[1])
    assert data["ecs"]["total"] == 1 and data["ecs"]["with_metrics"] == 0
    assert data["capacity"]["gaussdb_disk"] == data["capacity"]["ecs_disk"] == data["capacity"]["ecs_cpu"] == []
    assert data["capacity"]["dcs_memory"] == [{"name": "原生缓存", "value": 0, "resource_id": 4}]
    assert data["tenants"][0]["metrics_source"] == "AOM" and data["tenants"][0]["metrics_effective"] == "NONE"
    capacity = builder._capacity(old_resources, [1])
    assert len(capacity) == 1 and capacity[0]["name"] == "原生缓存" and capacity[0]["value"] == 0
    report = builder.build_report(old_resources, "DAILY", NOW-timedelta(days=1), NOW, tenant_id=1)
    assert report["capacity"] == capacity


def test_ledger_xlsx_blanks_only_current_aom_metrics_preserving_native_zero(old_resources):
    types = ["ecs_server", "dcs_instance", "gaussdb_instance"]
    workbook = load_workbook(BytesIO(export_ledger(old_resources, types, tenant_ids=[1])))
    for rtype in types:
        sheet = workbook[RESOURCE_TYPES[rtype]["name"]]
        for row in sheet.iter_rows(min_row=2, values_only=True):
            name = row[1]
            for index, column in enumerate(RESOURCE_TYPES[rtype]["columns"], start=len(COMMON)):
                if column["key"].startswith("metrics."):
                    expected = None
                    if name == "原生缓存":
                        expected = current_metrics(old_resources.get(Resource, 4)).get(column["key"][8:])
                    assert row[index] == expected, (rtype, name, column["key"], row[index])
            assert name != "其他租户"
    assert not any(cell.data_type == "f" for sheet in workbook for row in sheet for cell in row)


def test_dcs_panel_does_not_treat_old_aom_values_as_native_current_snapshot(old_resources):
    native = old_resources.get(Resource, 4)
    original_attrs = native.attrs
    native.attrs = {**native.attrs, "metrics_source": "DCS", "dcs_monitoring": {
        "source": "DCS", "api_version": "v2", "endpoint": "instances/statistic",
        "observed_at": NOW.isoformat(), "time_basis": "RESPONSE_RECEIVED", "stale_after_sec": 900}}
    try:
        with old_resources.no_autoflush:
            data = dcs_history_service.list_instances(old_resources, visibility_reader())
    finally:
        native.attrs = original_attrs
    rows = {row.id: row for row in data.items}
    assert set(rows) == {2, 4}
    assert rows[2].memory_usage is rows[2].used_memory_mb is rows[2].max_memory_mb is None
    assert rows[4].memory_usage == 0 and rows[4].used_memory_mb == 0 and rows[4].max_memory_mb == 1024
