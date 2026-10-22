"""角色权限与数据范围：内置角色、自定义角色 CRUD（实时生效）、只读访客、租户范围（X-Tenant-Id / 用户租户范围）、操作审计。"""
from .conftest import demo_login, login

VIEWER_PERMS = ["alarm:view", "dashboard:view", "ledger:view", "notify:view", "podevent:view", "report:view",
                "rule:view", "task:view"]


def _roles(client, admin) -> dict[str, dict]:
    return {r["code"]: r for r in client.get("/api/v1/system/roles", headers=admin).json()["data"]}


def test_builtin_roles_and_permission_catalog(client, admin):
    roles = _roles(client, admin)
    assert list(roles) == ["admin", "ops_lead", "oncall", "viewer"] and all(r["builtin"] for r in roles.values())
    assert roles["admin"]["permissions"] == ["*"] and sorted(roles["viewer"]["permissions"]) == VIEWER_PERMS
    assert "audit:view" in roles["ops_lead"]["permissions"] and "audit:view" not in roles["oncall"]["permissions"]
    assert "tenant:manage" not in roles["ops_lead"]["permissions"]
    groups = client.get("/api/v1/system/permissions", headers=admin).json()["data"]
    codes = [i["code"] for g in groups for i in g["items"]]
    assert len(codes) == len(set(codes)) == 28 and {"tenant:manage", "podevent:config", "audit:view"} <= set(codes)
    assert {"silence:manage", "report:config", "notify:manage", "task:edit", "collect:manage", "setting:manage"} <= set(codes)
    assert "silence:manage" in roles["oncall"]["permissions"] and "collect:manage" not in roles["oncall"]["permissions"]
    assert "setting:manage" not in roles["ops_lead"]["permissions"] and "collect:manage" in roles["ops_lead"]["permissions"]


def test_custom_role_lifecycle(client, admin):
    body = {"code": "auditor", "name": "审计员", "description": "只看审计与总览",
            "permissions": ["audit:view", "dashboard:view"]}
    r = client.post("/api/v1/system/roles", headers=admin, json=body)
    assert r.status_code == 200 and r.json()["data"]["builtin"] is False
    rid = r.json()["data"]["id"]
    assert client.post("/api/v1/system/roles", headers=admin, json=body).status_code == 409
    bad = client.post("/api/v1/system/roles", headers=admin, json={**body, "code": "bad", "permissions": ["nope:x"]})
    assert bad.status_code == 400
    star = client.post("/api/v1/system/roles", headers=admin, json={**body, "code": "root2", "permissions": ["*"]})
    assert star.status_code == 400
    u = client.post("/api/v1/system/users", headers=admin, json={
        "username": "auditor01", "display_name": "审计员", "password": "Init-pass-01", "role_id": rid})
    assert u.status_code == 200
    h = login(client, "auditor01", "Init-pass-01")
    assert client.get("/api/v1/system/audit-logs", headers=h).status_code == 200
    assert client.get("/api/v1/alarms", headers=h).status_code == 403
    patched = client.patch(f"/api/v1/system/roles/{rid}", headers=admin,
                           json={"permissions": ["audit:view", "alarm:view"]})
    assert patched.status_code == 200 and sorted(patched.json()["data"]["permissions"]) == ["alarm:view", "audit:view"]
    assert client.get("/api/v1/alarms", headers=h).status_code == 200      # 权限每次请求按角色计算，无需重新登录
    assert client.delete(f"/api/v1/system/roles/{rid}", headers=admin).status_code == 409   # 仍有用户使用
    builtin = _roles(client, admin)
    assert client.patch(f"/api/v1/system/roles/{builtin['admin']['id']}", headers=admin,
                        json={"permissions": []}).status_code == 400
    assert client.delete(f"/api/v1/system/roles/{builtin['viewer']['id']}", headers=admin).status_code == 400
    client.patch(f"/api/v1/system/users/{u.json()['data']['id']}", headers=admin,
                 json={"role_id": builtin["viewer"]["id"]})
    assert client.delete(f"/api/v1/system/roles/{rid}", headers=admin).status_code == 200
    page = client.get("/api/v1/system/audit-logs", params={"action": ["role.*"], "page_size": 50},
                      headers=admin).json()["data"]
    assert {"role.create", "role.update", "role.delete"} <= {a["action"] for a in page["items"]}


def test_viewer_is_read_only(client):
    v = demo_login(client, "viewer")
    me = client.get("/api/v1/auth/me", headers=v).json()["data"]
    assert sorted(me["permissions"]) == VIEWER_PERMS
    for url in ("/api/v1/alarms", "/api/v1/resources", "/api/v1/pod-events", "/api/v1/reports", "/api/v1/rules",
                "/api/v1/system/tasks"):
        assert client.get(url, headers=v).status_code == 200, url
    forbidden = (
        ("post", "/api/v1/alarms/batch-ack", {"ids": [1]}),
        ("patch", "/api/v1/resources/1", {"owner": "x"}),
        ("post", "/api/v1/resources/sync", {}),
        ("get", "/api/v1/resources/export", None),
        ("post", "/api/v1/reports", {"kind": "DAILY"}),
        ("post", "/api/v1/pod-events/probes", {}),
        ("post", "/api/v1/pod-events/collect", {}),
        ("post", "/api/v1/system/tasks/ecs_status/run", None),
        ("get", "/api/v1/system/tenants", None),
    )
    for method, url, body in forbidden:
        r = client.request(method.upper(), url, json=body, headers=v)
        assert r.status_code == 403 and r.json()["code"] == 40301, url


def test_ops_lead_can_audit_but_not_manage_accounts(client):
    lead = demo_login(client, "zhaolei")
    assert client.get("/api/v1/system/audit-logs", headers=lead).status_code == 200
    assert client.get("/api/v1/system/tenants", headers=lead).status_code == 200
    for method, url in (("get", "/api/v1/system/users"), ("get", "/api/v1/system/roles"),
                        ("post", "/api/v1/system/tenants/1/test")):
        assert client.request(method.upper(), url, headers=lead).status_code == 403, url


def test_tenant_scoped_users(client, admin, tenant_ids):
    a, b = tenant_ids["tenant-a"], tenant_ids["tenant-b"]
    lina, wangfang = demo_login(client, "lina"), demo_login(client, "wangfang")
    assert client.get("/api/v1/resources/summary", headers=lina).json()["data"]["total"] == 138
    assert client.get("/api/v1/resources/summary", headers=wangfang).json()["data"]["total"] == 58
    denied = client.get("/api/v1/resources/summary", headers={**lina, "X-Tenant-Id": str(b)})
    assert denied.status_code == 403 and denied.json()["code"] == 40301
    res = client.get("/api/v1/resources", params={"page_size": 100}, headers=lina).json()["data"]["items"]
    assert {x["tenant_id"] for x in res} == {a}
    alarms = client.get("/api/v1/alarms", params={"page_size": 100}, headers=wangfang).json()["data"]
    as_admin = client.get("/api/v1/alarms", params={"page_size": 100}, headers={**admin, "X-Tenant-Id": str(b)})
    assert alarms["items"] and {x["tenant_id"] for x in alarms["items"]} == {b}
    assert alarms["total"] == as_admin.json()["data"]["total"]
    a_alarm = client.get("/api/v1/alarms", params={"page_size": 1},
                         headers={**admin, "X-Tenant-Id": str(a)}).json()["data"]["items"][0]
    assert client.get(f"/api/v1/alarms/{a_alarm['id']}", headers=wangfang).status_code == 404   # 范围外 = 不存在
    assert client.post(f"/api/v1/alarms/{a_alarm['id']}/ack", headers=wangfang).status_code == 404
    pods = client.get("/api/v1/pod-events", params={"page_size": 100}, headers=wangfang).json()["data"]["items"]
    assert pods and {p["tenant_id"] for p in pods} == {b}
    dash = client.get("/api/v1/dashboard/summary", headers=wangfang).json()["data"]
    assert [t["code"] for t in dash["tenants"]] == ["tenant-b"]
    assert [t["code"] for t in client.get("/api/v1/system/tenants", headers=lina).json()["data"]] == ["tenant-a"]
    assert client.get("/api/v1/alarms", headers={**admin, "X-Tenant-Id": "abc"}).status_code == 400


def test_audit_log_records_login_and_failures(client, admin):
    client.post("/api/v1/auth/login", json={"username": "viewer", "password": "wrong-password"})
    page = client.get("/api/v1/system/audit-logs", params={"action": ["auth.login_failed"], "keyword": "viewer"},
                      headers=admin).json()["data"]
    assert page["total"] >= 1 and page["items"][0]["result"] == "FAILED"
    ok = client.get("/api/v1/system/audit-logs", params={"action": ["auth.login"], "result": "SUCCESS"},
                    headers=admin).json()["data"]
    assert ok["total"] > 0 and all(i["action_name"] == "登录" for i in ok["items"])
    assert {"code": "tenant.update", "name": "修改租户"} in ok["actions"]
    scoped = client.get("/api/v1/system/audit-logs", params={"page_size": 200},
                        headers=demo_login(client, "zhaolei")).json()["data"]
    assert scoped["total"] > 0
