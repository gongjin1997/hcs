"""多租户接入：AUTO/Prometheus 选择与旧 AOM 停用、显式旧 CES 兼容、认证隔离与 CRUD 脱敏。"""
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.crypto import encrypt
from app.hcs import factory, signer
from app.ledger.sync import sync_ledger
from app.metrics import effective
from app.models import EcsMetricSample, InspectionTask, Resource
from app.services.pipeline import run_task
from app.services.tenancy import resolve_targets
from app.services.tenant_check import check_tenant
from mock_hcs import tenants as mt
from mock_hcs.app import app as mock_app

from .conftest import MOCK_URL as MOCK

NOW = datetime(2026, 10, 21, 8, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def monitoring_defaults(monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", False)
    monkeypatch.setattr(get_settings(), "hcs_mock_url", None)
    monkeypatch.setattr(get_settings(), "hcs_endpoints", {k: v for k, v in get_settings().hcs_endpoints.items() if k != "aom"})


def _aom_mapping(db):
    task = db.get(InspectionTask, "ecs_metrics")
    task.params = {**(task.params or {}), "aom": {"namespace": "CUSTOM.HCS_INSPECTOR_TEST", "dimension": "instance_id",
                   "metrics": {name: name for name in ("cpu_util", "mem_util", "disk_util")},
                   "period": 300, "window_sec": 900}}
    db.commit()


def _target(db, code):
    return next(t for t in resolve_targets(db) if t.code == code)


def _sync(db, code, http):
    tg = _target(db, code)
    return sync_ledger(db, client=tg.client(http=http, mock_url=MOCK), target=tg, trigger="MANUAL")


def _run(db, code, task, http):
    tg = _target(db, code)
    return run_task(db, task, client=tg.client(http=http, mock_url=MOCK), target=tg, trigger="MANUAL")


def _ecs(db, tenant):
    return list(db.scalars(select(Resource).where(Resource.tenant_id == tenant.id,
                                                  Resource.resource_type == "ecs_server")))


def _sample_times(response):
    """Read actual fake-upstream times without substituting test/collection time."""
    response.read()
    body = response.json()
    times = {datetime.fromtimestamp(point["timestamp"] / 1000, UTC)
             for row in body.get("samples", []) for point in row.get("data_points", [])}
    times.update(datetime.fromtimestamp(point["timestamp"] / 1000, UTC)
                 for row in body.get("metrics", []) for point in row.get("datapoints", []))
    times.update(datetime.fromtimestamp(row["value"][0], UTC)
                 for row in (body.get("data") or {}).get("result", []) if "value" in row)
    return times


def test_signer_canonical_request_and_verify():
    url = "https://ecs.region-01.hcs.example.com/v1/p1/cloudservers/detail?offset=1&limit=100"
    headers = {"Content-Type": "application/json"}
    signed = signer.sign("GET", url, headers, b"", "test-ak", "test-sk", now=NOW)
    assert signed["X-Sdk-Date"] == "20261021T080000Z"
    canonical = "\n".join([
        "GET", "/v1/p1/cloudservers/detail/", "limit=100&offset=1",
        "content-type:application/json\nhost:ecs.region-01.hcs.example.com\nx-sdk-date:20261021T080000Z\n",
        "content-type;host;x-sdk-date", hashlib.sha256(b"").hexdigest()])
    sts = f"SDK-HMAC-SHA256\n20261021T080000Z\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    expected = hmac.new(b"test-sk", sts.encode(), hashlib.sha256).hexdigest()
    assert signed["Authorization"] == ("SDK-HMAC-SHA256 Access=test-ak, SignedHeaders=content-type;host;x-sdk-date, "
                                       f"Signature={expected}")
    req = {**headers, "Host": "ecs.region-01.hcs.example.com", **signed}
    path, query = "/v1/p1/cloudservers/detail", [("offset", "1"), ("limit", "100")]
    lookup = {"test-ak": "test-sk"}.get
    assert signer.verify("GET", path, query, req, b"", lookup, now=NOW + timedelta(minutes=5)) == "test-ak"
    with pytest.raises(ValueError, match="签名不匹配"):
        signer.verify("GET", path, [("offset", "2"), ("limit", "100")], req, b"", lookup, now=NOW)
    with pytest.raises(ValueError, match="15 分钟"):
        signer.verify("GET", path, query, req, b"", lookup, now=NOW + timedelta(minutes=20))
    with pytest.raises(ValueError, match="AK"):
        signer.verify("GET", path, query, req, b"", {}.get, now=NOW)
    assert signer.canonical_uri("/v1/a b/c") == "/v1/a%20b/c/"


def test_password_and_aksk_tenants_sync_separately(world, mem_db, mock_http):
    checks = {code: check_tenant(mem_db, t, http=mock_http, mock_url=MOCK) for code, t in world.items()}
    assert checks["tenant-a"]["checks"][0]["name"] == "IAM 认证（用户名密码换 Token）"
    assert checks["tenant-b"]["checks"][0]["name"] == "IAM 认证（AK/SK 签名）"
    assert all(c["ok"] for r in checks.values() for c in r["checks"] if c["kind"] != "capability")
    runs = {code: _sync(mem_db, code, mock_http) for code in world}
    assert {c: (r.status, r.resource_total) for c, r in runs.items()} == {"tenant-a": ("SUCCESS", 138),
                                                                        "tenant-b": ("SUCCESS", 58)}
    counts = dict(mem_db.execute(select(Resource.tenant_id, func.count()).group_by(Resource.tenant_id)).all())
    assert counts == {world["tenant-a"].id: 138, world["tenant-b"].id: 58}


def test_wrong_secret_fails_only_that_tenant(world, mem_db, mock_http):
    world["tenant-b"].secret_cipher = encrypt("wrong-secret")
    mem_db.commit()
    run = _run(mem_db, "tenant-b", "ecs_status", mock_http)
    assert run.status == "FAILED" and "签名不匹配" in run.error_summary
    assert _run(mem_db, "tenant-a", "ecs_status", mock_http).status == "SUCCESS"
    check = check_tenant(mem_db, world["tenant-b"], http=mock_http, mock_url=MOCK)
    assert check["ok"] is False and check["checks"][0]["kind"] == "auth" and len(check["checks"]) == 1


def test_auto_ignores_legacy_aom_and_keeps_prometheus_for_other_tenant(world, mem_db, mock_http, monkeypatch):
    a, b = world["tenant-a"], world["tenant-b"]
    a.metrics_source, a.endpoints, a.prometheus_url = "AUTO", {"aom": f"{MOCK}/aom"}, None
    b.metrics_source = "PROMETHEUS"
    _aom_mapping(mem_db)
    calls, observed = [], set()
    original = mock_http.send
    def track(request, *args, **kwargs):
        calls.append(request.url.path)
        response = original(request, *args, **kwargs)
        observed.update(_sample_times(response))
        return response
    monkeypatch.setattr(mock_http, "send", track)
    for t in (a, b):
        check_tenant(mem_db, t, http=mock_http, mock_url=MOCK)
    # 旧地址和映射不能重新启用 AOM；另一个租户的 Prometheus 不受影响。
    assert (effective(a), effective(b)) == ("NONE", "PROMETHEUS")
    assert "aom" not in a.capabilities and b.capabilities["prometheus"]["available"] is True
    assert all("ces" not in t.capabilities for t in (a, b))
    for code in world:
        _sync(mem_db, code, mock_http)
        assert _run(mem_db, code, "ecs_metrics", mock_http).status == "SUCCESS"
        before_status = len(calls)
        assert _run(mem_db, code, "ecs_status", mock_http).status == "SUCCESS"
        assert all("/ams/" not in path and not path.endswith("/samples") and "/api/v1/query" not in path
                   and not path.startswith("/V1.0/") for path in calls[before_status:])
    assert {r.attrs["metrics_source"] for r in _ecs(mem_db, a)} == {"NONE"}
    assert {r.attrs["metrics_source"] for r in _ecs(mem_db, b)} == {"PROMETHEUS"}
    with_cpu = {t.code: sum(1 for r in _ecs(mem_db, t) if (r.metrics or {}).get("cpu_util") is not None) for t in (a, b)}
    assert with_cpu == {"tenant-a": 0, "tenant-b": 13}          # 运行中的云服务器都有使用率（关机的没有）
    assert all("/ams/" not in path for path in calls)
    assert all(not path.endswith("/samples") for path in calls)
    assert all(not path.startswith("/V1.0/") for path in calls)
    history = list(mem_db.scalars(select(EcsMetricSample)))
    assert history and {row.observed_at for row in history} <= observed
    assert {row.source for row in history} == {"PROMETHEUS"}
    assert all(row.observed_at <= row.collected_at for row in history)


def test_auto_without_configured_monitoring_keeps_native_state_and_missing_metrics(world, mem_db, mock_http, monkeypatch):
    a = world["tenant-a"]
    a.metrics_source, a.endpoints, a.prometheus_url, a.capabilities = "AUTO", {}, None, {}
    _sync(mem_db, "tenant-a", mock_http)
    tg = _target(mem_db, "tenant-a")
    current = tg.client(http=mock_http, mock_url=MOCK)
    current.resolver.templates.pop("aom", None)  # 明确模拟未注册 AOM 的局点，不改变全局 Mock fixture。
    paths = []
    original = mock_http.send
    def track(request, *args, **kwargs):
        paths.append(request.url.path)
        return original(request, *args, **kwargs)
    monkeypatch.setattr(mock_http, "send", track)
    sample_run = run_task(mem_db, "ecs_metrics", client=current, target=tg, trigger="MANUAL")
    assert sample_run.status == "SUCCESS" and sample_run.stats["metrics_diagnostic"]["status"] == "no_source"
    run = run_task(mem_db, "ecs_status", client=current, target=tg, trigger="MANUAL")
    assert run.status == "SUCCESS" and effective(a) == "NONE" and a.capabilities == {}
    assert {r.attrs["metrics_source"] for r in _ecs(mem_db, a)} == {"NONE"}
    assert {r.status_raw for r in _ecs(mem_db, a)} == {"ACTIVE", "SHUTOFF"}
    assert all((r.metrics or {}).get(name) is None for r in _ecs(mem_db, a) for name in ("cpu_util", "mem_util", "disk_util"))
    again = run_task(mem_db, "ecs_status", client=current, target=tg, trigger="MANUAL")
    assert again.status == "SUCCESS" and again.api_calls == 1
    assert all("/ams/" not in path and not path.startswith("/V1.0/") for path in paths)
    assert mem_db.scalar(select(func.count()).select_from(EcsMetricSample)) == 0


def test_legacy_ces_is_only_used_with_explicit_platform_and_tenant_opt_in(world, mem_db, mock_http, monkeypatch):
    a = world["tenant-a"]
    a.metrics_source = "CES"
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    monkeypatch.setitem(mt.TENANT_A, "services", [*mt.TENANT_A["services"], "ces"])
    paths, observed = [], set()
    original = mock_http.send
    def track(request, *args, **kwargs):
        paths.append(request.url.path)
        response = original(request, *args, **kwargs)
        observed.update(_sample_times(response))
        return response
    monkeypatch.setattr(mock_http, "send", track)
    _sync(mem_db, "tenant-a", mock_http)
    sample_run = _run(mem_db, "tenant-a", "ecs_metrics", mock_http)
    assert sample_run.status == "SUCCESS", sample_run.error_summary
    before_status = len(paths)
    run = _run(mem_db, "tenant-a", "ecs_status", mock_http)
    assert run.status == "SUCCESS" and effective(a) == "CES"
    assert {r.attrs["metrics_source"] for r in _ecs(mem_db, a)} == {"CES"}
    assert sum((r.metrics or {}).get("cpu_util") is not None for r in _ecs(mem_db, a)) == 31
    assert any(path.startswith("/V1.0/") for path in paths)
    assert all(not path.startswith("/V1.0/") for path in paths[before_status:])
    history = list(mem_db.scalars(select(EcsMetricSample)))
    assert history and {row.observed_at for row in history} <= observed
    assert {row.source for row in history} == {"CES"}


def test_tenant_api_masks_secrets_and_validates(client, admin):
    raw = client.get("/api/v1/system/tenants", headers=admin)
    b = next(t for t in raw.json()["data"] if t["code"] == "tenant-b")
    assert b["auth_type"] == "AKSK" and b["secret_set"] is True and "*" in b["access_key_mask"]
    assert "secret" not in b and "access_key" not in b
    assert mt.TENANT_B["secret_key"] not in raw.text and mt.TENANT_B["access_key"] not in raw.text
    body = {"code": "tenant-c", "name": "租户C · 测试", "short_name": "租户C", "iam_endpoint": "https://iam.example.com",
            "domain_name": "hcs_test_c", "auth_type": "AKSK", "access_key": "TESTAKC0000000000001",
            "projects": [{"region_id": "region-01", "project_id": "p-tenant-c", "external_domain": "hcs.example.com"}]}
    r = client.post("/api/v1/system/tenants", json=body, headers=admin)
    assert r.status_code == 400 and "SK" in r.json()["message"]
    r = client.post("/api/v1/system/tenants", json={**body, "secret": "test-secret-c", "metrics_source": "PROMETHEUS"},
                    headers=admin)
    assert r.status_code == 400 and "Prometheus 地址" in r.json()["message"]
    r = client.post("/api/v1/system/tenants", json={**body, "secret": "test-secret-c"}, headers=admin)
    assert r.status_code == 200, r.text
    created = r.json()["data"]
    try:
        assert created["secret_set"] is True and created["access_key_mask"] != body["access_key"]
        assert "test-secret-c" not in r.text and created["metrics_effective"] == "NONE"
        audit = client.get("/api/v1/system/audit-logs", params={"action": ["tenant.create"]}, headers=admin)
        assert audit.json()["data"]["items"][0]["target_name"] == "租户C · 测试" and "test-secret-c" not in audit.text
        prom = client.put(f"/api/v1/system/tenants/{created['id']}", headers=admin,
                          json={**body, "metrics_source": "PROMETHEUS", "prometheus_url": "https://prom.example.invalid"})
        assert prom.status_code == 200 and prom.json()["data"]["metrics_effective"] == "PROMETHEUS"
        aom = client.put(f"/api/v1/system/tenants/{created['id']}", headers=admin,
                         json={**body, "metrics_source": "AOM", "endpoints": {"aom": "https://aom.example.invalid"}})
        assert aom.status_code == 400 and "AOM 已全局停用" in aom.text
        assert "test-secret-c" not in prom.text + aom.text
        keep = client.put(f"/api/v1/system/tenants/{created['id']}", json={**body, "remark": "改备注"}, headers=admin)
        assert keep.status_code == 200 and keep.json()["data"]["secret_set"] is True
        same_ak = client.put(f"/api/v1/system/tenants/{created['id']}", json={**body, 'access_key': None}, headers=admin)
        assert same_ak.status_code == 200 and same_ak.json()['data']['access_key_mask'] == created['access_key_mask']
        no_ak = client.post('/api/v1/system/tenants', headers=admin,
                            json={**body, 'code': 'tenant-d', 'name': '租户D', 'access_key': ''})
        assert no_ak.status_code == 400 and 'Access Key' in no_ak.json()['message']
        switch = client.put(f"/api/v1/system/tenants/{created['id']}",
                            json={**body, "auth_type": "PASSWORD", "username": "inspector_ro"}, headers=admin)
        assert switch.status_code == 400
    finally:
        cleanup = client.delete(f"/api/v1/system/tenants/{created['id']}", headers=admin)
        assert cleanup.status_code in (200, 404)
    busy = client.delete(f"/api/v1/system/tenants/{b['id']}", headers=admin)
    assert busy.status_code == 409 and "停用" in busy.json()["message"]


def test_tenant_connectivity_api(client, admin, tenant_ids, monkeypatch):
    """接口触发的连通性测试走同一套探测逻辑（这里把 HTTP 客户端换成进程内 Mock HCS）。"""
    monkeypatch.setattr(factory, "_http", lambda s: TestClient(mock_app, base_url=MOCK))
    monkeypatch.setattr(get_settings(), "hcs_mock_url", MOCK)
    r = client.post(f"/api/v1/system/tenants/{tenant_ids['tenant-b']}/test", headers=admin).json()["data"]
    states = {c["name"]: c.get("state", c["ok"]) for c in r["checks"]}
    assert r["ok"] is True and r["hcs_version"] == "8.5.1" and r["metrics_source"] == "PROMETHEUS"
    assert states["IAM 认证（AK/SK 签名）"] is True and "云监控 CES" not in states
    assert states.get("应用运维管理 AOM", "skipped") == "skipped"
    assert states["Prometheus"] == "available" and states["CCE K8s API"] == "available"
    log = client.get("/api/v1/system/audit-logs", params={"action": ["tenant.test"], "page_size": 1},
                     headers=admin).json()["data"]["items"][0]
    assert log["result"] == "SUCCESS" and log["tenant_name"] == "租户B"
