"""本地 ECS PDF §5.10 契约与项目隔离；所有上游请求由 MockTransport 接管。"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, local
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from app.api.v1.ecs_quotas import router
from app.core.config import get_settings
from app.core.db import Base, _make_engine, get_db
from app.core.deps import CurrentUser, get_current_user, scope_of
from app.core.response import BizError
from app.hcs import HcsError, ratelimit
from app.hcs.auth import AkSkAuth
from app.hcs.client import HcsClient
from app.hcs.endpoints import EndpointResolver
from app.main import app as production_app
from app.models import AuditLog, CollectSwitch, HcsAccount, HcsProject, Resource, SysSetting
from app.schemas.ecs_quotas import EcsQuotaOut, EcsQuotasOut
from app.services import ecs_quotas as service
from app.services.tenancy import Target

BASE = "/api/v1/ecs/quotas"
VALUES = {"maxTotalInstances": 10, "totalInstancesUsed": 4, "maxTotalCores": 32, "totalCoresUsed": 8,
          "maxTotalRAMSize": 65536, "totalRAMUsed": 16384}


class Upstream:
    def __init__(self):
        self.payload = {"absolute": dict(VALUES)}
        self.status = 200
        self.error = None
        self.calls = []
        self.targets = []
        self.clients = []

    def respond(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, json=self.payload)

    def client_for(self, tenant, project):
        self.targets.append((tenant.id, project.id))
        client = HcsClient(
            httpx.Client(transport=httpx.MockTransport(self.respond)),
            AkSkAuth("synthetic-ak", "synthetic-sk", project_id=project.project_id),
            EndpointResolver({"ecs": "https://ecs.example.invalid"}, project.region_id, "example.invalid"),
            project.project_id, tenant_id=tenant.id, project_ref=project.id, max_retries=0,
        )
        self.clients.append(client)
        return client


@pytest.fixture()
def quota_api(mem_db, monkeypatch):
    for tid in (1, 2):
        mem_db.add(HcsAccount(id=tid, code=f"quota-{tid}", name=f"租户{tid}",
                             domain_name=f"domain-{tid}", iam_endpoint="https://iam.example.invalid"))
    mem_db.flush()
    # 不同租户、区域刻意复用同一个上游 project_id，验证本地外键隔离。
    for pid, tid, region in ((11, 1, "region-1"), (12, 1, "region-2"), (21, 2, "region-1")):
        mem_db.add(HcsProject(id=pid, account_id=tid, region_id=region, project_id="cloud-project",
                             project_name=f"项目{pid}", external_domain="example.invalid"))
    mem_db.commit()
    upstream = Upstream()
    monkeypatch.setattr(service, "client_for", upstream.client_for)
    monkeypatch.setattr(get_settings(), "hcs_mock_url", None)
    monkeypatch.setattr(get_settings(), "hcs_endpoints", {"ecs": "https://ecs.example.invalid"})
    monkeypatch.setattr(get_settings(), "redis_url", None)
    monkeypatch.setattr(get_settings(), "hcs_total_qps", 100000)
    monkeypatch.setattr(ratelimit, "_shared", {})
    user = CurrentUser(id=1, username="quota-reader", display_name="配额读者",
                       permissions=["ledger:view", "ledger:sync"], tenant_ids=[1])
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        yield client, user, upstream
    for cloud in upstream.clients:
        cloud.http.close()


def quota(data, key="instances"):
    return next(item for item in data["quotas"] if item["key"] == key)


def get_project(client, pid=11, **params):
    response = client.get(BASE, params={"project_ref": pid, **params})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    EcsQuotasOut.model_validate(data)
    return data["items"][0]


def test_cache_only_view_includes_unknown_projects_without_cloud_or_writes(mem_db, quota_api):
    client, _, cloud = quota_api
    response = client.get(BASE)
    assert response.status_code == 200
    data = EcsQuotasOut.model_validate(response.json()["data"])
    assert [row.project_ref for row in data.items] == [11, 12]
    assert data.total == 2
    assert cloud.calls == cloud.targets == []
    assert mem_db.scalar(select(func.count()).select_from(SysSetting)) == 0
    assert mem_db.scalar(select(func.count()).select_from(AuditLog)) == 0
    for item in data.items:
        assert item.status == "not_collected"
        assert item.fetched_at is item.last_attempt_at is item.source_observed_at is None
        assert all(q.limit is q.used is q.remaining is None for q in item.quotas)


def test_refresh_native_contract_timestamps_cache_and_audit(mem_db, quota_api, monkeypatch):
    client, _, cloud = quota_api
    first = datetime(2026, 11, 2, tzinfo=UTC)
    times = iter((first, first + timedelta(seconds=2)))
    monkeypatch.setattr(service, "utcnow", lambda: next(times))
    response = client.post(f"{BASE}/11/refresh", params={"tenant_id": 1})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    result = EcsQuotaOut.model_validate(data)
    assert result.tenant_id == 1 and result.project_ref == 11 and result.project_id == "cloud-project"
    assert result.last_attempt_at == first and result.fetched_at == first + timedelta(seconds=2)
    assert result.source_observed_at is None
    assert data["status"] == "partial"  # 未返回的其他配额不默认为 0。
    assert quota(data)["limit"] == 10 and quota(data)["used"] == 4 and quota(data)["remaining"] == 6
    assert quota(data, "cores")["unit"] == "vCPU"
    assert quota(data, "ram")["used"] == 16384 and quota(data, "ram")["unit"] == "MB"
    assert len(cloud.calls) == 1 and cloud.targets == [(1, 11)]
    assert cloud.calls[0].method == "GET"
    assert cloud.calls[0].url.path == "/v1/cloud-project/cloudservers/limits"
    assert not cloud.calls[0].url.query
    assert cloud.clients[0].http.is_closed
    assert get_project(client) == data
    assert get_project(client, 12)["status"] == "not_collected"
    assert len(cloud.calls) == 1
    row = mem_db.get(SysSetting, service.cache_key(1, 11))
    assert row is not None and row.value["absolute"]["totalInstancesUsed"] == 4
    log = mem_db.scalar(select(AuditLog))
    assert log.action == "ecs.quota.refresh" and log.tenant_id == 1 and log.target_id == "11"
    assert log.result == "SUCCESS" and log.detail["project_ref"] == 11


@pytest.mark.parametrize("raw", [-1, "-1"])
def test_unlimited_limit_is_not_zero_or_negative_usage(raw):
    result = service.normalize_absolute({"maxTotalInstances": raw, "totalInstancesUsed": "0"})[0]
    assert result.limit_status == "unlimited" and result.limit is None and result.used == 0
    assert result.usage_status == "known" and result.remaining is None
    assert json.loads(result.model_dump_json())["limit"] is None


@pytest.mark.parametrize("raw", [None, True, False, 2.5, 2.0, "2.5", "", "NaN", "Infinity", -2, {}, [], "1e3"])
def test_malformed_limit_and_usage_are_unknown(raw):
    result = service.normalize_absolute({"maxTotalInstances": raw, "totalInstancesUsed": raw})[0]
    assert result.limit is result.used is result.remaining is None
    assert result.limit_status == result.usage_status == "unknown"


def test_zero_overage_negative_usage_and_limit_only_fields():
    zero = service.normalize_absolute({"maxTotalInstances": 0, "totalInstancesUsed": 0})[0]
    assert zero.limit_status == zero.usage_status == "known" and zero.remaining == 0
    over = service.normalize_absolute({"maxTotalInstances": "2", "totalInstancesUsed": "4"})[0]
    assert over.remaining == -2
    negative = service.normalize_absolute({"maxTotalInstances": 2, "totalInstancesUsed": -1})[0]
    assert negative.used is None and negative.usage_status == "unknown"
    fields = {q.key: q for q in service.normalize_absolute({"maxTotalKeypairs": 5})}
    assert fields["key_pairs"].usage_status == "not_reported" and fields["key_pairs"].used is None
    assert "VPC" in fields["security_groups"].note
    assert "VPC" in fields["security_group_rules"].note


def test_all_native_fields_and_unknown_extensions(quota_api):
    client, _, cloud = quota_api
    absolute = {q.limit: -1 for q in service.QUOTAS}
    absolute.update({q.used: 0 for q in service.QUOTAS if q.used})
    absolute["unexpected_secret"] = "do-not-store-or-return"
    cloud.payload = {"absolute": absolute, "token": "do-not-store-or-return"}
    response = client.post(f"{BASE}/11/refresh")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "available" and len(data["quotas"]) == 13
    assert all(q["limit_status"] == "unlimited" for q in data["quotas"])
    assert "do-not-store-or-return" not in response.text


def test_cache_isolated_by_tenant_and_local_project_even_for_same_cloud_id(mem_db, quota_api):
    client, user, cloud = quota_api
    user.tenant_ids = None
    for tid, pid, used in ((1, 11, 1), (1, 12, 2), (2, 21, 3)):
        cloud.payload = {"absolute": {**VALUES, "totalInstancesUsed": used}}
        assert client.post(f"{BASE}/{pid}/refresh", params={"tenant_id": tid}).status_code == 200
    user.tenant_ids = [1]
    assert quota(get_project(client, 11))["used"] == 1
    assert quota(get_project(client, 12))["used"] == 2
    assert client.get(BASE, params={"project_ref": 21}).json()["data"]["items"] == []
    assert client.get(BASE, params={"tenant_id": 2}).status_code == 404
    assert client.post(f"{BASE}/21/refresh").status_code == 404
    assert client.post(f"{BASE}/21/refresh", params={"tenant_id": 1}).status_code == 404
    assert client.post(f"{BASE}/11/refresh", params={"tenant_id": 2}).status_code == 404
    assert len(cloud.calls) == 3
    assert {row.key for row in mem_db.scalars(select(SysSetting))} == {
        service.cache_key(1, 11), service.cache_key(1, 12), service.cache_key(2, 21)}


def test_selected_tenant_header_and_empty_scope_never_fall_back(quota_api):
    client, user, cloud = quota_api
    user.tenant_ids = None
    response = client.get(BASE, headers={"X-Tenant-Id": "2"})
    assert [r["project_ref"] for r in response.json()["data"]["items"]] == [21]
    assert client.post(f"{BASE}/11/refresh", headers={"X-Tenant-Id": "2"}).status_code == 404
    user.tenant_ids = [1]
    assert client.get(BASE, headers={"X-Tenant-Id": "2"}).status_code == 403
    user.tenant_ids = []
    assert client.get(BASE).json()["data"]["items"] == []
    assert client.post(f"{BASE}/11/refresh").status_code == 404
    assert cloud.calls == cloud.targets == []


@pytest.mark.parametrize("perms", [[], ["ledger:view"], ["tenant:manage"], ["task:view"]])
def test_refresh_requires_sync_or_task_run_permission(mem_db, quota_api, perms):
    client, user, cloud = quota_api
    user.permissions = perms
    assert client.post(f"{BASE}/11/refresh").status_code == 403
    assert cloud.calls == []
    assert mem_db.scalar(select(func.count()).select_from(SysSetting)) == 0
    assert mem_db.scalar(select(func.count()).select_from(AuditLog)) == 0


@pytest.mark.parametrize("perms", [["ledger:sync"], ["task:run"]])
def test_either_refresh_permission_is_sufficient(quota_api, perms):
    client, user, cloud = quota_api
    user.permissions = perms
    assert client.get(BASE).status_code == 403
    assert client.post(f"{BASE}/11/refresh").status_code == 200
    assert len(cloud.calls) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
def test_upstream_failure_preserves_old_snapshot_and_sanitizes_audit(mem_db, quota_api, status):
    client, _, cloud = quota_api
    before = client.post(f"{BASE}/11/refresh").json()["data"]
    cloud.status = status
    cloud.payload = {"error": {"code": "private-code", "message": "token=must-not-leak"}}
    response = client.post(f"{BASE}/11/refresh")
    assert response.status_code == 502, response.text
    assert response.json()["code"] == 50201
    data = response.json()["data"]
    assert data["status"] == "refresh_failed" and data["last_error"] == "upstream_error" and data["is_stale"]
    assert data["fetched_at"] == before["fetched_at"] and data["quotas"] == before["quotas"]
    assert get_project(client) == data
    assert get_project(client, 12)["status"] == "not_collected"
    logs = list(mem_db.scalars(select(AuditLog).order_by(AuditLog.id)))
    assert [log.result for log in logs] == ["SUCCESS", "FAILED"]
    assert "must-not-leak" not in response.text + json.dumps([log.detail for log in logs])
    assert len(cloud.calls) == 2  # 无兼容接口/defaults/其他项目回退。


@pytest.mark.parametrize("error", [httpx.ConnectError("private-host token=secret"),
                                   HcsError(0, "NETWORK", "private-host token=secret")])
def test_first_failure_is_unknown_not_zero(quota_api, error):
    client, _, cloud = quota_api
    cloud.error = error
    response = client.post(f"{BASE}/11/refresh")
    assert response.status_code == 502
    data = response.json()["data"]
    assert data["status"] == "refresh_failed" and data["fetched_at"] is None and not data["is_stale"]
    assert all(q["used"] is q["limit"] is None for q in data["quotas"])
    assert data["last_attempt_at"] is not None and "token=secret" not in response.text


@pytest.mark.parametrize("payload", [{}, {"limits": {"absolute": VALUES}}, {"quota_set": VALUES},
                                     {"absolute": None}, {"absolute": []}])
def test_native_shape_is_required_without_compatibility_or_default_fallback(quota_api, payload):
    client, _, cloud = quota_api
    cloud.payload = payload
    response = client.post(f"{BASE}/11/refresh")
    assert response.status_code == 502
    assert response.json()["data"]["last_error"] == "invalid_response"
    assert len(cloud.calls) == 1


def test_malformed_values_stay_null_in_json_and_cached_reads(quota_api):
    client, _, cloud = quota_api
    cloud.payload = {"absolute": {"maxTotalInstances": None, "totalInstancesUsed": True,
                                 "maxTotalCores": "NaN", "totalCoresUsed": -1,
                                 "maxTotalRAMSize": -1, "totalRAMUsed": "512"}}
    response = client.post(f"{BASE}/11/refresh")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "partial"
    assert quota(data)["limit"] is quota(data)["used"] is None
    assert quota(data, "cores")["used"] is None
    assert quota(data, "ram")["limit_status"] == "unlimited" and quota(data, "ram")["used"] == 512
    assert get_project(client) == data


def test_limits_without_usage_do_not_invent_resource_counts(mem_db, quota_api):
    client, _, cloud = quota_api
    mem_db.add(Resource(tenant_id=1, project_ref=11, service="ecs", resource_type="ecs_server",
                        resource_id="synthetic-server", name="合成台账资源", region="region-1"))
    mem_db.commit()
    cloud.payload = {"absolute": {"maxTotalInstances": 10}}
    response = client.post(f"{BASE}/11/refresh")
    assert response.status_code == 200
    item = quota(response.json()["data"])
    assert item["limit"] == 10 and item["used"] is None and item["usage_status"] == "unknown"
    assert quota(get_project(client))["used"] is None


def test_invalid_envelope_preserves_snapshot_and_recovery_clears_failure(quota_api, monkeypatch):
    client, _, cloud = quota_api
    start = datetime(2026, 11, 2, tzinfo=UTC)
    times = iter(start + timedelta(seconds=i) for i in range(5))
    monkeypatch.setattr(service, "utcnow", lambda: next(times))
    first = client.post(f"{BASE}/11/refresh").json()["data"]
    cloud.payload = {"absolute": None}
    failure = client.post(f"{BASE}/11/refresh")
    assert failure.status_code == 502
    old = failure.json()["data"]
    assert old["fetched_at"] == first["fetched_at"] and old["quotas"] == first["quotas"]
    assert old["last_error"] == "invalid_response" and old["is_stale"]
    cloud.payload = {"absolute": {**VALUES, "totalInstancesUsed": 5}}
    recovered = client.post(f"{BASE}/11/refresh").json()["data"]
    assert recovered["last_error"] is None and not recovered["is_stale"]
    assert recovered["fetched_at"] != first["fetched_at"] and quota(recovered)["used"] == 5
    assert get_project(client) == recovered


def test_configuration_failure_is_safe_and_audited(mem_db, quota_api, monkeypatch):
    client, _, cloud = quota_api

    def invalid_configuration(*args, **kwargs):
        raise ValueError("private endpoint and token must not leak")

    monkeypatch.setattr(service, "client_for", invalid_configuration)
    response = client.post(f"{BASE}/11/refresh")
    assert response.status_code == 502
    assert response.json()["data"]["last_error"] == "configuration_error"
    assert "private endpoint" not in response.text
    assert mem_db.scalar(select(AuditLog)).result == "FAILED"
    assert cloud.calls == []


@pytest.mark.parametrize("model", [HcsAccount, HcsProject])
def test_disabled_target_is_visible_but_not_refreshed(mem_db, quota_api, model):
    client, _, cloud = quota_api
    mem_db.get(model, 1 if model is HcsAccount else 11).enabled = False
    mem_db.commit()
    assert not get_project(client)["enabled"]
    assert client.post(f"{BASE}/11/refresh").status_code == 409
    assert cloud.calls == []
    assert mem_db.scalar(select(AuditLog)).result == "FAILED"


@pytest.mark.parametrize("switch_tenant", [0, 1])
def test_master_off_rejects_manual_and_scheduler_before_client_creation(mem_db, quota_api, switch_tenant):
    client, user, cloud = quota_api
    before = client.post(f"{BASE}/11/refresh").json()["data"]
    cached = dict(mem_db.get(SysSetting, service.cache_key(1, 11)).value)
    target = service.resolve_target(mem_db, scope_of(user, None), 11)
    mem_db.add(CollectSwitch(tenant_id=switch_tenant, resource_type="ecs_server", enabled=False))
    mem_db.commit()
    calls, clients = len(cloud.calls), len(cloud.clients)
    response = client.post(f"{BASE}/11/refresh")
    assert response.status_code == 409
    with pytest.raises(BizError, match="主采集开关"):
        service.refresh_quota(mem_db, target)
    assert (len(cloud.calls), len(cloud.clients)) == (calls, clients)
    assert mem_db.get(SysSetting, service.cache_key(1, 11)).value == cached
    data = get_project(client)
    assert not data["enabled"] and data["quotas"] == before["quotas"]
    assert data["fetched_at"] == before["fetched_at"]
    assert list(mem_db.scalars(select(AuditLog).order_by(AuditLog.id)))[-1].result == "FAILED"


def test_master_gate_uses_existing_tenant_override_semantics(mem_db, quota_api):
    client, _, cloud = quota_api
    mem_db.add_all([CollectSwitch(tenant_id=0, resource_type="ecs_server", enabled=False),
                    CollectSwitch(tenant_id=1, resource_type="ecs_server", enabled=True),
                    CollectSwitch(tenant_id=2, resource_type="ecs_server", enabled=False)])
    mem_db.commit()
    assert client.post(f"{BASE}/11/refresh").status_code == 200
    assert cloud.targets == [(1, 11)]


def test_explicit_manual_refresh_bypasses_disabled_automatic_quota_stage(mem_db, quota_api):
    from app.services.collection_policy import policy_key

    client, _, cloud = quota_api
    mem_db.add(SysSetting(key=policy_key(1), value={"quota": {"enabled": False, "interval_sec": 86400}}))
    mem_db.commit()
    assert client.post(f"{BASE}/11/refresh").status_code == 200
    assert cloud.targets == [(1, 11)]


@pytest.mark.parametrize("changed", ["project_id", "region_id", "external_domain"])
def test_reconfigured_project_does_not_show_previous_target_cache(mem_db, quota_api, changed):
    client, _, cloud = quota_api
    assert client.post(f"{BASE}/11/refresh").status_code == 200
    setattr(mem_db.get(HcsProject, 11), changed, "new-value")
    mem_db.commit()
    data = get_project(client)
    assert data["status"] == "not_collected" and data["fetched_at"] is None
    assert quota(data)["used"] is None
    assert len(cloud.calls) == 1


def test_corrupt_or_foreign_cache_never_becomes_a_known_snapshot(mem_db, quota_api):
    client, _, _ = quota_api
    assert client.post(f"{BASE}/11/refresh").status_code == 200
    own = mem_db.get(SysSetting, service.cache_key(1, 11))
    mem_db.add(SysSetting(key=service.cache_key(1, 12), value=own.value))
    mem_db.commit()
    assert get_project(client, 12)["status"] == "not_collected"
    own.value = {**own.value, "fetched_at": "not-a-timestamp"}
    mem_db.commit()
    assert get_project(client)["status"] == "not_collected"


def test_scheduler_entry_uses_injected_client_without_committing_or_closing(mem_db, quota_api):
    _, user, cloud = quota_api
    target = service.resolve_target(mem_db, scope_of(user, None), 11)
    injected = cloud.client_for(target.tenant, target.project)
    data = service.refresh_quota(mem_db, target, injected)
    assert data.project_ref == 11 and data.status == "partial"
    assert not injected.http.is_closed
    assert mem_db.scalar(select(func.count()).select_from(AuditLog)) == 0
    mem_db.rollback()
    assert mem_db.get(SysSetting, service.cache_key(1, 11)) is None


def test_injected_foreign_client_and_legacy_target_are_rejected_without_queries(mem_db, quota_api):
    _, user, cloud = quota_api
    target = service.resolve_target(mem_db, scope_of(user, None), 11)
    foreign = cloud.client_for(mem_db.get(HcsAccount, 2), mem_db.get(HcsProject, 21))
    with pytest.raises(BizError, match="不匹配"):
        service.refresh_quota(mem_db, target, foreign)
    target.tenant_id = None
    with pytest.raises(BizError):
        service.refresh_quota(mem_db, target)
    assert cloud.calls == []
    assert mem_db.scalar(select(func.count()).select_from(SysSetting)) == 0


def test_empty_database_never_uses_environment_tenant(mem_db, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("空数据库不能使用环境变量凭据")
    monkeypatch.setattr(service, "client_for", forbidden)
    user = CurrentUser(id=1, username="test", display_name="test", tenant_ids=None)
    assert service.list_quotas(mem_db, scope_of(user, None)).items == []
    legacy = Target(tenant_id=None, code="legacy", name="legacy", short_name="", project_ref=None,
                    project_id="legacy", project_name="legacy", region="region")
    with pytest.raises(BizError):
        service.refresh_quota(mem_db, legacy)


def test_pagination_and_filters_are_bounded(quota_api):
    client, _, cloud = quota_api
    result = client.get(BASE, params={"tenant_id": 1, "page": 2, "page_size": 1}).json()["data"]
    assert result["total"] == 2 and result["items"][0]["project_ref"] == 12
    for params in ({"page_size": 101}, {"page": 0}, {"tenant_id": 0}, {"project_ref": -1}):
        assert client.get(BASE, params=params).status_code == 400
    assert client.post(f"{BASE}/0/refresh").status_code == 400
    assert cloud.calls == []


@pytest.fixture()
def concurrent_quota_db(migration_url, monkeypatch):
    """独立连接池：默认真实 SQLite WAL；显式测试 PG URL 下同一组测试运行于隔离 PG schema。"""
    engine = _make_engine(migration_url)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        db.add(HcsAccount(id=1, code="concurrent", name="并发测试", domain_name="synthetic-domain",
                          iam_endpoint="https://iam.example.invalid"))
        db.flush()
        db.add(HcsProject(id=11, account_id=1, region_id="region-1", project_id="cloud-project",
                          external_domain="example.invalid"))
        db.commit()
    monkeypatch.setattr(get_settings(), "hcs_mock_url", None)
    monkeypatch.setattr(get_settings(), "hcs_endpoints", {"ecs": "https://ecs.example.invalid"})
    monkeypatch.setattr(get_settings(), "redis_url", None)
    monkeypatch.setattr(get_settings(), "hcs_total_qps", 100000)
    monkeypatch.setattr(ratelimit, "_shared", {})

    def forbidden_factory(*args, **kwargs):
        pytest.fail("并发测试只能使用已注入的 MockTransport 客户端")

    monkeypatch.setattr(service, "client_for", forbidden_factory)
    clock = local()
    real_now = service.utcnow
    monkeypatch.setattr(service, "utcnow", lambda: next(clock.times) if hasattr(clock, "times") else real_now())
    user = CurrentUser(id=1, username="concurrent", display_name="并发测试", tenant_ids=[1])

    def refresh(respond, *, times=None):
        if times is not None:
            clock.times = iter(times)
        try:
            with sessions() as db:
                target = service.resolve_target(db, scope_of(user, None), 11)
                with httpx.Client(transport=httpx.MockTransport(respond)) as http:
                    client = HcsClient(http, AkSkAuth("synthetic-ak", "synthetic-sk", project_id="cloud-project"),
                                       EndpointResolver({"ecs": "https://ecs.example.invalid"}, "region-1", "example.invalid"),
                                       "cloud-project", tenant_id=1, project_ref=11, max_retries=0,
                                       qps={"ecs": 100000})
                    result = service.refresh_quota(db, target, client)
                    db.commit()
                    return result
        finally:
            if hasattr(clock, "times"):
                del clock.times

    try:
        yield sessions, user, refresh
    finally:
        engine.dispose()


def test_concurrent_initial_refresh_upserts_one_row(concurrent_quota_db):
    sessions, user, refresh = concurrent_quota_db
    barrier = Barrier(2)
    start = datetime(2026, 11, 2, tzinfo=UTC)

    def response(used):
        def respond(request):
            barrier.wait(timeout=5)
            return httpx.Response(200, json={"absolute": {**VALUES, "totalInstancesUsed": used}})
        return respond

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(refresh, response(1), times=[start, start + timedelta(seconds=2)])
        second = pool.submit(refresh, response(2), times=[start + timedelta(seconds=1), start + timedelta(seconds=3)])
        assert first.result(timeout=10).fetched_at is not None
        assert second.result(timeout=10).fetched_at is not None
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(SysSetting)) == 1
        result = service.list_quotas(db, scope_of(user, None)).items[0]
        assert result.quotas[0].used == 2
        assert result.last_attempt_at == start + timedelta(seconds=1)
        assert result.fetched_at == start + timedelta(seconds=3)


@pytest.mark.parametrize(("older_status", "newer_status"), [(200, 200), (503, 200), (200, 503), (503, 503)])
def test_manual_scheduler_races_keep_newest_attempt_and_success(concurrent_quota_db, older_status, newer_status):
    sessions, user, refresh = concurrent_quota_db
    start = datetime(2026, 11, 2, tzinfo=UTC)
    baseline = refresh(lambda request: httpx.Response(200, json={"absolute": {**VALUES, "totalInstancesUsed": 1}}),
                       times=[start - timedelta(seconds=2), start - timedelta(seconds=1)])
    entered, release = Event(), Event()

    def older(request):
        entered.set()
        assert release.wait(5)
        return httpx.Response(older_status, json={"absolute": {**VALUES, "totalInstancesUsed": 3}})

    def newer(request):
        return httpx.Response(newer_status, json={"absolute": {**VALUES, "totalInstancesUsed": 7}})

    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(refresh, older, times=[start, start + timedelta(seconds=4)])
        try:
            assert entered.wait(5)
            fast = pool.submit(refresh, newer, times=[start + timedelta(seconds=1), start + timedelta(seconds=2)])
            fast.result(timeout=5)
        finally:
            release.set()
        slow.result(timeout=5)
    with sessions() as db:
        result = service.list_quotas(db, scope_of(user, None)).items[0]
        assert result.last_attempt_at == start + timedelta(seconds=1)
        assert result.last_error == (None if newer_status == 200 else "upstream_error")
        assert result.is_stale == (newer_status != 200)
        expected_used = 7 if newer_status == 200 else (3 if older_status == 200 else 1)
        expected_at = (start + timedelta(seconds=2) if newer_status == 200 else
                       start + timedelta(seconds=4) if older_status == 200 else baseline.fetched_at)
        assert result.quotas[0].used == expected_used and result.fetched_at == expected_at


@pytest.mark.parametrize("disabled", ["master", "tenant", "project"])
def test_reader_session_reloads_cache_and_master_after_other_session_commit(concurrent_quota_db, disabled):
    sessions, user, refresh = concurrent_quota_db
    refresh(lambda request: httpx.Response(200, json={"absolute": VALUES}))
    with sessions() as reader:
        old_cache = reader.get(SysSetting, service.cache_key(1, 11))
        old_tenant = reader.get(HcsAccount, 1)
        old_project = reader.get(HcsProject, 11)
        target = service.resolve_target(reader, scope_of(user, None), 11)
        with sessions() as writer:
            switch = CollectSwitch(tenant_id=1, resource_type="ecs_server", enabled=True)
            writer.add(switch)
            writer.commit()
            switch_id = switch.id
        old_switch = reader.get(CollectSwitch, switch_id)
        refresh(lambda request: httpx.Response(200, json={"absolute": {**VALUES, "totalInstancesUsed": 9}}))
        with sessions() as writer:
            if disabled == "master":
                writer.get(CollectSwitch, switch_id).enabled = False
            elif disabled == "tenant":
                writer.get(HcsAccount, 1).enabled = False
            else:
                writer.get(HcsProject, 11).enabled = False
            writer.commit()
        assert old_switch.enabled and old_tenant.enabled and old_project.enabled
        with pytest.raises(BizError) as blocked:
            service.refresh_quota(reader, target)
        assert blocked.value.http_status == 409
        result = service.list_quotas(reader, scope_of(user, None)).items[0]
        assert result.quotas[0].used == 9 and not result.enabled
        assert old_cache.value["absolute"]["totalInstancesUsed"] == 9


def test_postgres_cache_write_uses_on_conflict_and_row_lock(mem_db, quota_api):
    """无 PG 服务时仍验证实际写入函数发出的 PostgreSQL 语句；真并发测试也可在 PG 模式运行。"""
    _, user, _ = quota_api
    target = service.resolve_target(mem_db, scope_of(user, None), 11)
    captured = []
    class RecordingDb:
        def get_bind(self):
            return SimpleNamespace(dialect=postgresql.dialect())

        def execute(self, statement):
            captured.append(str(statement.compile(dialect=postgresql.dialect())))
            parameters = statement.compile().params
            self.row = SysSetting(key=parameters["key"], value=parameters["value"])

        def scalar(self, statement):
            captured.append(str(statement.compile(dialect=postgresql.dialect())))
            return self.row

        def flush(self):
            pass

    now = datetime(2026, 11, 2, tzinfo=UTC)
    candidate = service._Cache(identity=service._identity(target.tenant, target.project), last_attempt_at=now,
                               fetched_at=now, successful_attempt_at=now, absolute=dict(VALUES))
    service._store_cache(RecordingDb(), target.tenant, target.project, candidate)
    assert "ON CONFLICT (key) DO NOTHING" in captured[0]
    assert any("FOR UPDATE" in statement for statement in captured[1:])
