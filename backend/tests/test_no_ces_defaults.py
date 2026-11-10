"""HCS 8.5.1 默认不请求 CES；只使用明确配置的可选指标来源。"""
from collections import OrderedDict
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app import metrics
from app.api.v1.system import info
from app.collectors.base import RunContext
from app.collectors.dcs import DcsCollector
from app.collectors.ecs import EcsStatusCollector
from app.collectors.ecs_metrics import EcsMetricsCollector
from app.collectors.gaussdb import GaussDBCollector, GaussDBComponentsCollector, GaussDBStorageCollector
from app.collectors.mrs import MrsClusterCollector
from app.core.config import Settings, get_settings
from app.hcs import auth, client_for
from app.ledger.sync import sync_ledger
from app.models import AuditLog, EcsMetricSample, HcsProject, InspectionTask, Resource
from app.schemas.system import TenantIn
from app.services import tenant_check
from app.services.pipeline import run_task
from app.services.tenancy import resolve_targets
from app.services.tenant_check import check_tenant

from .test_user_tenant_review import management_api as management_api


@pytest.fixture(autouse=True)
def metric_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "hcs_ces_enabled", False)
    monkeypatch.setattr(settings, "hcs_endpoints", {k: v for k, v in settings.hcs_endpoints.items() if k != "aom"})
    monkeypatch.setattr(settings, "hcs_mock_url", None)
    monkeypatch.setattr(settings, "env", "test")
    return settings


def tenant(source="AUTO", *, endpoints=None, prometheus_url=None, capabilities=None):
    return SimpleNamespace(metrics_source=source, endpoints=endpoints or {}, prometheus_url=prometheus_url,
                           capabilities=capabilities or {})


def context(account):
    client = SimpleNamespace(mock_url=None, resolver=SimpleNamespace(templates={}))
    return RunContext(client=client, account="isolated", project_id="isolated-project", tenant=account)


def test_ces_setting_defaults_off(monkeypatch):
    monkeypatch.delenv("INSPECTOR_HCS_CES_ENABLED", raising=False)
    assert Settings(_env_file=None).hcs_ces_enabled is False


@pytest.mark.parametrize("source", ["AUTO", "AOM", "CES", "PROMETHEUS", "NONE"])
def test_no_source_can_enable_ces_without_explicit_platform_opt_in(source):
    account = tenant(source, capabilities={"ces": {"available": True}})
    called = []
    assert metrics.use_ces(account) is False
    assert metrics.guarded_ces(context(account), lambda: called.append("ces"), {}) == {}
    assert called == []


@pytest.mark.parametrize("source", ["AUTO", "AOM", "CES", "PROMETHEUS", "NONE"])
def test_ces_opt_in_still_requires_explicit_tenant_selection(source, metric_settings, monkeypatch):
    monkeypatch.setattr(metric_settings, "hcs_ces_enabled", True)
    assert metrics.use_ces(tenant(source)) is (source == "CES")


def test_explicit_legacy_ces_path_remains_available_only_after_opt_in(metric_settings, monkeypatch):
    monkeypatch.setattr(metric_settings, "hcs_ces_enabled", True)
    account = tenant("CES")
    assert metrics.guarded_ces(context(account), lambda: {"sample": 12}, {}) == {"sample": 12}
    assert account.capabilities["ces"]["available"] is True


def test_auto_ignores_legacy_ces_discovery_and_selects_only_explicit_prometheus():
    account = tenant(capabilities={"ces": {"available": True}})
    assert metrics.effective(account) == "NONE"
    account.prometheus_url = "https://prometheus.example.invalid"
    assert metrics.effective(account) == "PROMETHEUS"
    account.prometheus_url = "   "
    assert metrics.effective(account) == "NONE"


@pytest.mark.parametrize("source", ["AUTO", "AOM"])
def test_configured_endpoint_never_enables_aom(source, metric_settings, monkeypatch):
    account = tenant(source)
    ctx = context(account)
    assert metrics.effective(account) == "NONE"
    monkeypatch.setitem(metric_settings.hcs_endpoints, "aom", "https://aom.example.invalid")
    assert metrics.effective(account) == "NONE"
    assert metrics.guarded_aom(ctx, lambda: pytest.fail("AOM callback must not execute"), {}) == {}
    assert ctx.errors == [] and account.capabilities == {}



def test_neither_global_nor_tenant_endpoint_can_enable_aom(metric_settings, monkeypatch):
    monkeypatch.setitem(metric_settings.hcs_endpoints, "aom", "https://global-aom.example.invalid")
    assert metrics.effective(tenant("AOM", endpoints={"aom": "https://tenant-aom.example.invalid"})) == "NONE"
    assert metrics.effective(tenant("AOM", endpoints={"aom": " "})) == "NONE"
    assert metrics.effective(None) == "NONE"



def test_aom_is_disabled_even_for_explicit_development_mock_endpoint(metric_settings, monkeypatch):
    ctx = context(tenant("AOM"))
    ctx.client.mock_url = "http://mock"
    ctx.client.resolver.templates["aom"] = "http://mock/aom"
    assert metrics.effective(ctx.tenant, client=ctx.client) == "NONE"
    monkeypatch.setattr(metric_settings, "env", "prod")
    assert metrics.effective(ctx.tenant, client=ctx.client) == "NONE"



def test_old_aom_mapping_and_prometheus_address_do_not_implicitly_switch_sources():
    account = tenant("AOM", endpoints={"aom": "https://aom.example.invalid"},
                     prometheus_url="https://prometheus.example.invalid")
    ctx = context(account)
    ctx.stats["aom_mapping_configured"] = True
    assert metrics.effective(account) == "NONE"
    assert metrics.guarded_aom(ctx, lambda: pytest.fail("old mapping must not be queried"), {}) == {}
    assert account.capabilities == {} and ctx.errors == []



def test_disabled_aom_never_calls_a_denied_query_or_makes_the_run_partial():
    account = tenant("AOM", endpoints={"aom": "https://aom.example.invalid"})
    ctx = context(account)
    def denied():
        pytest.fail("Disabled AOM must not attempt a permission check")
    assert metrics.guarded_aom(ctx, denied, []) == []
    assert ctx.errors == [] and account.capabilities == {} and metrics.effective(account) == "NONE"



def test_missing_mapping_does_not_claim_an_aom_query_succeeded():
    account = tenant("AOM", endpoints={"aom": "https://aom.example.invalid"})
    ctx = context(account)

    def no_mapping():
        ctx.stats["aom_mapping_configured"] = False
        return {}

    assert metrics.guarded_aom(ctx, no_mapping, {}) == {}
    assert account.capabilities == {} and ctx.errors == []


def test_old_aom_capabilities_are_disabled_for_every_tenant():
    first = tenant(endpoints={"aom": "https://aom.example.invalid"})
    second = tenant(endpoints={"aom": "https://aom.example.invalid"}, capabilities={"aom": {"available": True}})
    metrics.mark(first, "aom", False, "test unavailable")
    assert metrics.available(first, "aom") is False and metrics.available(second, "aom") is False
    assert first.capabilities == {} and second.capabilities == {"aom": {"available": True}}
    first.metrics_source = "AOM"
    assert metrics.effective(first) == metrics.effective(second) == "NONE"



@pytest.mark.parametrize("previous", [True, False])
@pytest.mark.parametrize("changed", ["tenant_endpoint", "global_endpoint", "secret_cipher", "username", "domain_name",
                                    "domain_id", "auth_type", "access_key", "iam_endpoint", "id"])
def test_changing_old_aom_endpoint_or_credentials_cannot_reenable_source(metric_settings, monkeypatch, previous, changed):
    account = tenant("AOM", endpoints={} if changed == "global_endpoint" else {"aom": "https://first-aom.example.invalid"},
                     capabilities={"aom": {"available": previous, "reason": "legacy record"}})
    account.id, account.auth_type, account.secret_cipher = 101, "PASSWORD", "synthetic-initial-cipher"
    account.username, account.domain_name, account.domain_id = "reader", "domain", "domain-id"
    account.access_key, account.iam_endpoint = None, "https://iam.example.invalid"
    before = dict(account.capabilities)
    assert metrics.available(account, "aom") is False
    if changed == "tenant_endpoint":
        account.endpoints = {"aom": "https://second-aom.example.invalid"}
    elif changed == "global_endpoint":
        monkeypatch.setitem(metric_settings.hcs_endpoints, "aom", "https://second-aom.example.invalid")
    else:
        setattr(account, changed, 202 if changed == "id" else "synthetic-changed-value")
    metrics.mark(account, "aom", True, "current configuration")
    assert metrics.available(account, "aom") is False and metrics.effective(account) == "NONE"
    assert account.capabilities == before



def test_unscoped_legacy_aom_record_cannot_enable_a_configured_endpoint():
    account = tenant("AOM", endpoints={"aom": "https://configured-aom.example.invalid"},
                     capabilities={"aom": {"available": True}})
    assert metrics.available(account, "aom") is False and metrics.effective(account) == "NONE"



def test_disabled_aom_does_not_reuse_partial_query_results():
    account = tenant("AOM", endpoints={"aom": "https://configured-aom.example.invalid"})
    ctx = context(account)
    def partial():
        pytest.fail("Disabled AOM must not retrieve a partial result")
    assert metrics.guarded_aom(ctx, partial, {}) == {}
    assert ctx.stats == {} and account.capabilities == {} and ctx.errors == []



def test_disabled_aom_does_not_execute_callbacks_that_report_errors():
    account = tenant("AOM", endpoints={"aom": "https://configured-aom.example.invalid"})
    ctx = context(account)
    def partial():
        ctx.errors.append("AOM sample request failed")
        pytest.fail("Disabled AOM callback executed")
    assert metrics.guarded_aom(ctx, partial, {}) == {}
    assert account.capabilities == {} and ctx.errors == []



def test_disabled_aom_does_not_attempt_an_unavailable_query_or_retry():
    account = tenant("AOM", endpoints={"aom": "https://configured-aom.example.invalid"})
    ctx = context(account)
    def unavailable():
        pytest.fail("Disabled AOM must not retry upstream")
    for _ in range(2):
        assert metrics.guarded_aom(ctx, unavailable, {}) == {}
    assert ctx.errors == [] and account.capabilities == {} and metrics.effective(account) == "NONE"



@pytest.mark.parametrize("available", [True, False, None])
@pytest.mark.parametrize("prometheus", [None, "https://prometheus.example.invalid"])
def test_auto_never_treats_aom_endpoint_or_api_capability_as_product_metric_evidence(available, prometheus):
    account = tenant(endpoints={"aom": "https://aom.example.invalid"}, prometheus_url=prometheus)
    if available is not None:
        metrics.mark(account, "aom", available, "generic metadata response")
    assert metrics.effective(account) == ("PROMETHEUS" if prometheus else "NONE")
    assert not metrics.use_aom(account)
    assert metrics.guarded_aom(context(account), lambda: pytest.fail("AUTO must not call generic AOM"), {}) == {}


@pytest.mark.parametrize("mapped,result", [(None, {"actual": 7}), (True, {}), (False, {"actual": 7})])
def test_old_successful_mapping_or_result_does_not_bypass_global_aom_policy(mapped, result):
    account = tenant("AOM", endpoints={"aom": "https://aom.example.invalid"})
    ctx = context(account)
    if mapped is not None:
        ctx.stats["aom_mapping_configured"] = mapped
    assert metrics.guarded_aom(ctx, lambda: pytest.fail(f"must not return old sample: {result}"), {}) == {}
    assert account.capabilities == {} and ctx.errors == []



@pytest.mark.parametrize("source", ["NONE", "CES", "PROMETHEUS"])
def test_explicit_sources_do_not_silently_switch_to_aom(source):
    assert metrics.effective(tenant(source, endpoints={"aom": "https://aom.example.invalid"})) == "NONE"


def tenant_body(**overrides):
    return {"code": "test-aom", "name": "isolated", "iam_endpoint": "https://iam.example.invalid",
            "projects": [{"region_id": "region-test", "project_id": "project-test",
                          "external_domain": "example.invalid"}], **overrides}


def test_aom_schema_rejects_source_even_with_a_global_endpoint(metric_settings, monkeypatch):
    monkeypatch.setitem(metric_settings.hcs_endpoints, "aom", "https://global-aom.example.invalid")
    with pytest.raises(ValidationError, match="AOM 已全局停用"):
        TenantIn.model_validate(tenant_body(metrics_source="AOM"))



def test_aom_schema_rejects_source_but_auto_allows_native_only():
    with pytest.raises(ValidationError, match="AOM 已全局停用"):
        TenantIn.model_validate(tenant_body(metrics_source="AOM"))
    assert TenantIn.model_validate(tenant_body()).metrics_source == "AUTO"



def test_aom_schema_rejects_source_for_both_real_and_blank_legacy_endpoints(metric_settings, monkeypatch):
    monkeypatch.setitem(metric_settings.hcs_endpoints, "aom", "https://global-aom.example.invalid")
    for endpoint in ("https://aom.example.invalid", " "):
        with pytest.raises(ValidationError, match="AOM 已全局停用"):
            TenantIn.model_validate(tenant_body(metrics_source="AOM", endpoints={"aom": endpoint}))



def test_aom_schema_rejects_source_in_both_mock_and_production_modes(metric_settings, monkeypatch):
    monkeypatch.setattr(metric_settings, "hcs_mock_url", "http://mock")
    for environment in ("test", "prod"):
        monkeypatch.setattr(metric_settings, "env", environment)
        with pytest.raises(ValidationError, match="AOM 已全局停用"):
            TenantIn.model_validate(tenant_body(metrics_source="AOM"))



def test_system_metadata_excludes_aom_and_keeps_ces_disabled_by_default():
    data = info()["data"]
    assert "AOM" not in data["metrics_sources"] and "aom" not in data["capabilities"]
    assert data["ces_enabled"] is False
    assert "不" in data["notes"]["ces"] and "全局停用" in data["notes"]["aom"]



@pytest.mark.parametrize("collector", [EcsStatusCollector, GaussDBCollector, GaussDBStorageCollector,
                                       GaussDBComponentsCollector, MrsClusterCollector])
def test_default_collectors_query_native_services_only(collector):
    calls = []

    class NativeClient:
        mock_url = None

        def get(self, service, path, **kwargs):
            calls.append((service, path))
            if service == "ecs":
                return {"count": 1, "servers": [{"id": "ecs-one", "name": "one", "status": "ACTIVE"}]}
            if service == "gaussdb":
                if path.endswith("/volume-usage"):
                    return {"used": "15", "total": "185"}
                if path.endswith("/components"):
                    return {"total_count": 1, "nodes": [{"id": "node-one", "components": [
                        {"id": "dn-one", "type": "DN", "role": "master", "status": "Primary"}]}]}
                return {"total_count": 1, "instances": [{"id": "db-one", "name": "one", "status": "ACTIVE"}]}
            if service == "mrs":
                return {"clusterTotal": 1, "clusters": [{"clusterId": "cluster-one", "clusterName": "one",
                                                        "clusterState": "running"}]}
            pytest.fail(f"unexpected monitoring service: {service}")

    ctx = context(tenant())
    ctx.client = NativeClient()
    rows = collector().collect(ctx)
    assert calls and all(service in {"ecs", "gaussdb", "mrs"} for service, _ in calls)
    assert ctx.errors == []
    assert all(row.check_item not in {"cpu", "memory"} for row in rows)
    assert all(row.check_item != "disk" or row.metrics["disk_usage"] == 8.11 for row in rows)
    if collector is GaussDBCollector:
        assert calls == [("gaussdb", "/gaussdb/v3.3/{project_id}/instances")]
        assert {row.check_item for row in rows} == {"instance_status"}
        assert rows[0].metrics == {"cpu_usage": None, "mem_usage": None}
    elif collector is GaussDBStorageCollector:
        assert calls == [("gaussdb", "/gaussdb/v3.3/{project_id}/instances"),
                         ("gaussdb", "/gaussdb/v3/{project_id}/instances/db-one/volume-usage")]
        assert {row.check_item for row in rows} == {"disk"}
        assert rows[0].storage_status == "AVAILABLE" and rows[0].status_raw is None
    elif collector is GaussDBComponentsCollector:
        assert calls == [("gaussdb", "/gaussdb/v3.3/{project_id}/instances"),
                         ("gaussdb", "/gaussdb/v3/{project_id}/instances/db-one/components")]
        assert {row.check_item for row in rows} == {"components"}
        assert next(row for row in rows if row.check_item == "components").facts["down_components"] == 0


@pytest.mark.parametrize("source", ["AUTO", "AOM", "CES", "PROMETHEUS", "NONE"])
def test_dcs_never_uses_ces_even_with_compatibility_enabled(source, metric_settings, monkeypatch):
    monkeypatch.setattr(metric_settings, "hcs_ces_enabled", True)
    calls = []

    class DcsClient:
        def get(self, service, path, **kwargs):
            calls.append((service, path))
            assert service == "dcs"
            if path == "/v2/{project_id}/instances/statistic":
                return {"statistics": [{"instance_id": "dcs-one", "used_memory": 20, "max_memory": 100}]}
            if path == "/v2/{project_id}/instances-logical-nodes":
                return {"count": 1, "instances": [{"instance_id": "dcs-one", "nodes": [
                    {"logical_node_id": "node-one", "node_type": "master", "node_role": "redis-server",
                     "status": "Active", "node_ip": "192.0.2.1", "node_port": 6379}]}]}
            assert path == "/v2/{project_id}/instances"
            return {"instance_num": 1, "instances": [{"instance_id": "dcs-one", "name": "one", "status": "RUNNING",
                                                     "used_memory": 20, "max_memory": 100}]}

    ctx = context(tenant(source, endpoints={"aom": "https://aom.example.invalid"}))
    ctx.client = DcsClient()
    rows = DcsCollector().collect(ctx)
    assert calls == [("dcs", "/v2/{project_id}/instances"), ("dcs", "/v2/{project_id}/instances/statistic"),
                     ("dcs", "/v2/{project_id}/instances-logical-nodes")]
    assert ctx.errors == []
    assert next(row for row in rows if row.check_item == "memory").facts["memory_usage"] == 20


@pytest.mark.parametrize("source,enabled", [("AUTO", False), ("AUTO", True), ("CES", False)])
def test_tenant_check_does_not_probe_unconfigured_monitoring_services(world, mem_db, mock_http, monkeypatch,
                                                                    metric_settings, source, enabled):
    monkeypatch.setattr(metric_settings, "hcs_ces_enabled", enabled)
    account = world["tenant-a"]
    account.metrics_source = source
    account.endpoints = {k: v for k, v in (account.endpoints or {}).items() if k != "aom"}
    account.capabilities = {}
    calls = []
    original = mock_http.send
    factory = tenant_check.client_for

    def unconfigured_client(*args, **kwargs):
        current = factory(*args, **kwargs)
        current.resolver.templates.pop("aom", None)  # 明确模拟“未配置 AOM”，不依赖开发 Mock 注册的服务集合。
        return current

    def track(request, *args, **kwargs):
        calls.append((request.method, request.url.path))
        return original(request, *args, **kwargs)

    monkeypatch.setattr(mock_http, "send", track)
    monkeypatch.setattr(tenant_check, "client_for", unconfigured_client)
    result = check_tenant(mem_db, account, http=mock_http, mock_url="http://mock")
    assert result["ok"] is True
    assert all(not path.startswith("/V1.0/") and "/ams/" not in path for _, path in calls)
    assert not any("CES" in item["name"] for item in result["checks"])
    assert "ces" not in (account.capabilities or {})


@pytest.mark.parametrize("status", [200, 403, 404])
@pytest.mark.parametrize("account_code", ["tenant-a", "tenant-b"])
def test_configured_aom_probe_is_skipped_without_upstream_requests(
        world, mem_db, mock_http, monkeypatch, metric_settings, status, account_code):
    monkeypatch.setattr(auth, "_tokens", OrderedDict())
    monkeypatch.setitem(metric_settings.hcs_endpoints, "aom", "https://configured-aom.example.invalid")
    account = world[account_code]
    account.metrics_source, account.capabilities = "AUTO", {}
    calls = []
    original = mock_http.send
    def track(request, *args, **kwargs):
        calls.append((request.method, request.url.path))
        if "/ams/" in request.url.path or "/aom/" in request.url.path:
            pytest.fail(f"disabled AOM reached an upstream configured to return {status}")
        return original(request, *args, **kwargs)
    monkeypatch.setattr(mock_http, "send", track)
    result = check_tenant(mem_db, account, http=mock_http, mock_url="http://mock")
    assert result["ok"] is True
    assert all(not path.startswith("/V1.0/") for _, path in calls)
    if account.auth_type == "PASSWORD":
        assert sum(path == "/v3/auth/tokens" for _, path in calls) == 1
    else:
        assert all(path != "/v3/auth/tokens" for _, path in calls)
    item = next(item for item in result["checks"] if "AOM" in item["name"])
    assert item["state"] == "skipped" and item["available"] is False and item["disabled"] is True
    assert "aom" not in account.capabilities
    assert result["metrics_source"] == ("PROMETHEUS" if account.prometheus_url else "NONE")



@pytest.mark.parametrize("collector", [EcsStatusCollector])
def test_old_aom_collector_configuration_keeps_only_native_facts_without_querying(collector, mem_db):
    calls = []
    class NativeClient:
        def get(self, service, path, **kwargs):
            calls.append((service, path))
            assert service == "ecs"
            return {"count": 1, "servers": [{"id": "one", "name": "one", "status": "ACTIVE"}]}
        def post(self, *args, **kwargs):
            pytest.fail("Disabled AOM must make zero metric requests")
    account = tenant("AOM", endpoints={"aom": "https://aom.example.invalid"})
    ctx = context(account)
    ctx.client = NativeClient()
    mem_db.add(Resource(service="ecs", resource_type="ecs_server", resource_id="one", name="one",
                        tenant_id=None, project_ref=None, attrs={}, metrics={}))
    mem_db.commit()
    ctx.db = mem_db
    sampled = EcsMetricsCollector().collect(ctx)
    assert len(sampled) == 1
    assert sampled[0].attrs["ecs_monitoring"]["diagnostic"]["status"] == "no_source"
    assert list(mem_db.scalars(select(EcsMetricSample))) == []
    rows = collector().collect(ctx)
    assert len(calls) == 1 and ctx.stats["metrics_source"] == "NONE"
    assert len(ctx.errors) == 1 and "AOM 已停用" in ctx.errors[0]
    assert account.capabilities == {}
    assert all(value is None for row in rows if row.check_item in {"cpu", "memory", "disk"}
               for key, value in row.facts.items() if key != "disk_mount")



@pytest.mark.parametrize("collector,fact", [(EcsStatusCollector, "cpu_util")])
@pytest.mark.parametrize("unit", ["Percent", "Count"])
def test_old_collector_mapping_cannot_trigger_catalogue_samples_or_alerts(
        world, mem_db, mock_http, monkeypatch, metric_settings, collector, fact, unit):
    monkeypatch.setitem(metric_settings.hcs_endpoints, "aom", "https://configured-aom.example.invalid")
    account = world["tenant-a"]
    account.metrics_source, account.capabilities = "AOM", {}
    project = mem_db.scalar(select(HcsProject).where(HcsProject.account_id == account.id).order_by(HcsProject.id).limit(1))
    native_calls = []
    original = mock_http.send
    def track(request, *args, **kwargs):
        path = request.url.path
        if "/aom/" in path or "/ams/" in path or path.endswith("/samples"):
            pytest.fail("Old product mappings must never reach AOM")
        if path.endswith("/cloudservers/detail"):
            native_calls.append(request)
            return httpx.Response(200, json={"count": 1, "servers": [
                {"id": "mapped-resource", "name": "one", "status": "ACTIVE"}]}, request=request)
        assert "/V1.0/" not in path
        return original(request, *args, **kwargs)
    monkeypatch.setattr(mock_http, "send", track)
    client = client_for(account, project, http=mock_http, mock_url="http://mock")
    target = next(target for target in resolve_targets(mem_db) if target.project_ref == project.id)
    task = mem_db.get(InspectionTask, "ecs_metrics")
    task.params = {"aom": {"namespace": "test.custom.metrics", "dimension": "server_ref",
                           "metrics": {fact: "site_cpu"}, "units": {fact: unit}}}
    mem_db.commit()
    inventory = sync_ledger(mem_db, client=client, target=target, types=["ecs_server"], trigger="MANUAL")
    assert inventory.status == "SUCCESS", inventory.error_summary
    sampled = run_task(mem_db, "ecs_metrics", client=client, target=target, trigger="MANUAL")
    status = run_task(mem_db, "ecs_status", client=client, target=target, trigger="MANUAL")
    assert sampled.status == "PARTIAL" and "AOM 已停用" in sampled.error_summary
    assert status.status == "SUCCESS", status.error_summary
    assert sampled.stats["metrics_source"] == status.stats["metrics_source"] == "NONE"
    assert len(native_calls) == 2
    resource = mem_db.scalar(select(Resource).where(Resource.tenant_id == account.id,
        Resource.project_ref == project.id, Resource.resource_type == "ecs_server"))
    assert resource.metrics["cpu_util"] is None
    assert list(mem_db.scalars(select(EcsMetricSample))) == []



def configured_task(db, params):
    task = InspectionTask(code="ecs_status", name="test ECS", service="ecs", interval_sec=120, enabled=True, params=params)
    db.add(task)
    db.commit()
    return task


def mapping():
    return {"namespace": "test.custom.metrics", "dimension": "server_ref", "metrics": {"cpu_util": "custom_cpu"}}


def test_task_patch_omitting_mapping_preserves_all_existing_params(mem_db, management_api):
    client, _, _, _ = management_api
    params = {"aom": mapping(), "window_hours": 6}
    task = configured_task(mem_db, params)
    response = client.put("/api/v1/system/tasks/ecs_status", json={"enabled": False})
    assert response.status_code == 200, response.text
    assert task.params == params and response.json()["data"]["params"] == {"timeout_sec": 300}
    assert not {"aom", "window_hours"} & response.json()["data"]["params"].keys()
    audit = mem_db.scalar(select(AuditLog).where(AuditLog.action == "task.update"))
    assert audit.detail["changes"] == {"enabled": [True, False]}


def test_task_patch_null_removes_only_aom_and_is_idempotent(mem_db, management_api):
    client, _, _, _ = management_api
    task = configured_task(mem_db, {"aom": mapping(), "window_hours": 6})
    response = client.put("/api/v1/system/tasks/ecs_status", json={"aom": None})
    assert response.status_code == 200, response.text
    assert task.params == {"window_hours": 6}
    assert response.json()["data"]["params"] == {"timeout_sec": 300}
    audit = mem_db.scalar(select(AuditLog).where(AuditLog.action == "task.update"))
    assert audit.detail["changes"] == {"aom": [mapping(), None]}
    again = client.put("/api/v1/system/tasks/ecs_status", json={"aom": None})
    assert again.status_code == 200 and again.json()["data"]["changed"] is False
    assert len(mem_db.scalars(select(AuditLog).where(AuditLog.action == "task.update")).all()) == 1


def test_task_mapping_update_still_requires_full_scope(mem_db, management_api):
    client, user, _, _ = management_api
    task = configured_task(mem_db, {"aom": mapping()})
    user.permissions, user.tenant_ids = ["task:edit"], [1]
    response = client.put("/api/v1/system/tasks/ecs_status", json={"aom": None})
    assert response.status_code == 403 and task.params == {"aom": mapping()}
    assert mem_db.scalar(select(AuditLog).where(AuditLog.action == "task.update")) is None


def test_new_task_mapping_is_rejected_without_editing_or_auditing_other_params(mem_db, management_api):
    client, _, _, _ = management_api
    params = {"window_hours": 6, "other_config": "unrelated-private-test-value"}
    task = configured_task(mem_db, params)
    response = client.put("/api/v1/system/tasks/ecs_status", json={"aom": mapping()})
    assert response.status_code == 400 and "AOM 已全局停用" in response.text
    assert task.params == params
    assert mem_db.scalar(select(AuditLog).where(AuditLog.action == "task.update")) is None



def test_old_task_mapping_is_preserved_privately_and_cannot_be_resubmitted(mem_db, management_api):
    client, _, _, _ = management_api
    configured = {**mapping(), "units": {"cpu_util": "Percent"}, "period": 60, "window_sec": 600,
                  "prometheus_instance_id": "test-prometheus"}
    task = configured_task(mem_db, {"window_hours": 6, "aom": configured})
    response = client.put("/api/v1/system/tasks/ecs_status", json={"aom": configured})
    assert response.status_code == 400
    assert task.params == {"window_hours": 6, "aom": configured}
    listed = client.get("/api/v1/system/tasks")
    assert listed.status_code == 200, listed.text
    public = next(row for row in listed.json()["data"] if row["code"] == task.code)
    assert public["params"] == {"timeout_sec": 300}
    assert not {"aom", "window_hours"} & public["params"].keys()
    assert "test-prometheus" not in listed.text and "custom_cpu" not in listed.text
    assert mem_db.scalar(select(AuditLog).where(AuditLog.action == "task.update")) is None



@pytest.mark.parametrize("invalid", [{"namespace": ""}, {"dimension": ""}, {"metrics": {}}, {"period": 0}, {"window_sec": 0}])
def test_invalid_task_mapping_cannot_partially_change_task(mem_db, management_api, invalid):
    client, _, _, _ = management_api
    task = configured_task(mem_db, {"window_hours": 6})
    response = client.put("/api/v1/system/tasks/ecs_status", json={"enabled": False, "aom": {**mapping(), **invalid}})
    assert response.status_code == 400, response.text
    assert task.enabled is True and task.params == {"window_hours": 6}
    assert mem_db.scalar(select(AuditLog).where(AuditLog.action == "task.update")) is None
