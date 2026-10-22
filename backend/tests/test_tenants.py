"""多租户接入：AK/SK 签名、两种认证方式、按租户采集、CES 自动探测 / 自动降级、Prometheus 兜底、连通性测试、租户管理接口。"""
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
from app.models import Resource
from app.services.pipeline import run_task
from app.services.tenancy import resolve_targets
from app.services.tenant_check import check_tenant
from mock_hcs import tenants as mt
from mock_hcs.app import app as mock_app

from .conftest import MOCK_URL as MOCK

NOW = datetime(2026, 10, 21, 8, 0, 0, tzinfo=UTC)


def _target(db, code):
    return next(t for t in resolve_targets(db) if t.code == code)


def _sync(db, code, http):
    tg = _target(db, code)
    return sync_ledger(db, client=tg.client(http=http, mock_url=MOCK), target=tg)


def _run(db, code, task, http):
    tg = _target(db, code)
    return run_task(db, task, client=tg.client(http=http, mock_url=MOCK), target=tg)


def _ecs(db, tenant):
    return list(db.scalars(select(Resource).where(Resource.tenant_id == tenant.id,
                                                  Resource.resource_type == "ecs_server")))


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


def test_ces_detection_and_prometheus_fallback(world, mem_db, mock_http):
    a, b = world["tenant-a"], world["tenant-b"]
    for t in (a, b):
        check_tenant(mem_db, t, http=mock_http, mock_url=MOCK)
    assert (effective(a), effective(b)) == ("CES", "PROMETHEUS")
    assert b.capabilities["ces"]["available"] is False and b.capabilities["prometheus"]["available"] is True
    for code in world:
        _sync(mem_db, code, mock_http)
        assert _run(mem_db, code, "ecs_status", mock_http).status == "SUCCESS"
    assert {r.attrs["metrics_source"] for r in _ecs(mem_db, a)} == {"CES"}
    assert {r.attrs["metrics_source"] for r in _ecs(mem_db, b)} == {"PROMETHEUS"}
    with_cpu = {t.code: sum(1 for r in _ecs(mem_db, t) if (r.metrics or {}).get("cpu_util") is not None) for t in (a, b)}
    assert with_cpu == {"tenant-a": 31, "tenant-b": 13}          # 运行中的云服务器都有使用率（关机的没有）


def test_ces_auto_degrade_when_missing(world, mem_db, mock_http, monkeypatch):
    a = world["tenant-a"]
    _sync(mem_db, "tenant-a", mock_http)
    original = list(mt.TENANT_A["services"])
    monkeypatch.setitem(mt.TENANT_A, "services", [s for s in original if s != "ces"])   # 模拟局点没有部署 CES
    run = _run(mem_db, "tenant-a", "ecs_status", mock_http)
    assert run.status == "SUCCESS"                                 # 没有 CES 不算巡检失败，状态巡检照常
    assert a.capabilities["ces"]["available"] is False and effective(a) == "NONE"
    assert {r.attrs["metrics_source"] for r in _ecs(mem_db, a)} == {"NONE"}
    assert {r.status_raw for r in _ecs(mem_db, a)} == {"ACTIVE", "SHUTOFF"}
    again = _run(mem_db, "tenant-a", "ecs_status", mock_http)
    assert again.status == "SUCCESS" and again.api_calls == 1     # 已标记不可用：只调用 ECS 列表，不再调用 CES
    monkeypatch.setitem(mt.TENANT_A, "services", original)        # 局点补装 CES 后，连通性测试重新探测即可恢复
    check_tenant(mem_db, a, http=mock_http, mock_url=MOCK)
    assert a.capabilities["ces"]["available"] is True and effective(a) == "CES"


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
    assert created["secret_set"] is True and created["access_key_mask"] != body["access_key"]
    assert "test-secret-c" not in r.text and created["metrics_effective"] == "CES"
    audit = client.get("/api/v1/system/audit-logs", params={"action": ["tenant.create"]}, headers=admin)
    assert audit.json()["data"]["items"][0]["target_name"] == "租户C · 测试" and "test-secret-c" not in audit.text
    keep = client.put(f"/api/v1/system/tenants/{created['id']}", json={**body, "remark": "改备注"}, headers=admin)
    assert keep.status_code == 200 and keep.json()["data"]["secret_set"] is True     # 密钥留空 = 不修改
    # 修改时 AK 留空 = 沿用原 AK（列表只返回脱敏值，前端无法回填）
    same_ak = client.put(f"/api/v1/system/tenants/{created['id']}", json={**body, 'access_key': None}, headers=admin)
    assert same_ak.status_code == 200 and same_ak.json()['data']['access_key_mask'] == created['access_key_mask']
    no_ak = client.post('/api/v1/system/tenants', headers=admin,
                        json={**body, 'code': 'tenant-d', 'name': '租户D', 'access_key': ''})
    assert no_ak.status_code == 400 and 'Access Key' in no_ak.json()['message']
    switch = client.put(f"/api/v1/system/tenants/{created['id']}",
                        json={**body, "auth_type": "PASSWORD", "username": "inspector_ro"}, headers=admin)
    assert switch.status_code == 400                                                  # 换认证方式要重填密钥
    assert client.delete(f"/api/v1/system/tenants/{created['id']}", headers=admin).status_code == 200
    busy = client.delete(f"/api/v1/system/tenants/{b['id']}", headers=admin)
    assert busy.status_code == 409 and "停用" in busy.json()["message"]


def test_tenant_connectivity_api(client, admin, tenant_ids, monkeypatch):
    """接口触发的连通性测试走同一套探测逻辑（这里把 HTTP 客户端换成进程内 Mock HCS）。"""
    monkeypatch.setattr(factory, "_http", lambda s: TestClient(mock_app, base_url=MOCK))
    monkeypatch.setattr(get_settings(), "hcs_mock_url", MOCK)
    r = client.post(f"/api/v1/system/tenants/{tenant_ids['tenant-b']}/test", headers=admin).json()["data"]
    states = {c["name"]: c.get("state", c["ok"]) for c in r["checks"]}
    assert r["ok"] is True and r["hcs_version"] == "8.5.1" and r["metrics_source"] == "PROMETHEUS"
    assert states["IAM 认证（AK/SK 签名）"] is True and states["云监控 CES"] == "missing"
    assert states["Prometheus"] == "available" and states["CCE K8s API"] == "available"
    log = client.get("/api/v1/system/audit-logs", params={"action": ["tenant.test"], "page_size": 1},
                     headers=admin).json()["data"]["items"][0]
    assert log["result"] == "SUCCESS" and log["tenant_name"] == "租户B"
