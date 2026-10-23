"""登录与账号：用户 = 角色 + 租户范围；连续输错锁定、7 天免登录、修改密码、停用即失效、管理员自我保护。"""
import itertools

from .conftest import login

_seq = itertools.count(1)
INIT = "Init-pass-01"          # 测试中新建账号的初始密码（仅测试库）


def _role_id(client, admin, code: str) -> int:
    roles = client.get("/api/v1/system/roles", headers=admin).json()["data"]
    return next(r["id"] for r in roles if r["code"] == code)


def _new_user(client, admin, password=INIT, role="oncall", **kw) -> tuple[int, str]:
    username = f"user{next(_seq):02d}"
    body = {"username": username, "display_name": "测试用户", "password": password,
            "role_id": _role_id(client, admin, role), **kw}
    r = client.post("/api/v1/system/users", headers=admin, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"], username


def test_remember_me_extends_token(client, admin):
    _, name = _new_user(client, admin)
    short = client.post("/api/v1/auth/login", json={"username": name, "password": INIT}).json()["data"]
    long = client.post("/api/v1/auth/login", json={"username": name, "password": INIT, "remember": True})
    assert long.json()["data"]["expires_in"] == 7 * 86400 > short["expires_in"]


def test_change_password(client, admin):
    _, name = _new_user(client, admin)
    h = login(client, name, INIT)
    bad = client.post("/api/v1/auth/password", headers=h, json={"old_password": "Wrong-pass-00",
                                                                 "new_password": "New-pass-0002"})
    assert bad.status_code == 400
    short = client.post("/api/v1/auth/password", headers=h, json={"old_password": INIT, "new_password": "short"})
    assert short.status_code == 400
    r = client.post("/api/v1/auth/password", headers=h, json={"old_password": INIT, "new_password": "New-pass-0002"})
    assert r.status_code == 200
    assert client.post("/api/v1/auth/login", json={"username": name, "password": INIT}).status_code == 401
    login(client, name, "New-pass-0002")


def test_lockout_after_failures_and_admin_reset(client, admin):
    uid, name = _new_user(client, admin)
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"username": name, "password": "Wrong-pass-00"})
    r = client.post("/api/v1/auth/login", json={"username": name, "password": INIT})
    assert r.status_code == 401 and "分钟后再试" in r.json()["message"]
    users = client.get("/api/v1/system/users", headers=admin).json()["data"]
    assert next(u for u in users if u["id"] == uid)["locked"] is True
    client.post(f"/api/v1/system/users/{uid}/reset-password", headers=admin, json={"password": "Reset-pass-03"})
    login(client, name, "Reset-pass-03")


def test_disable_user_revokes_token(client, admin):
    uid, name = _new_user(client, admin)
    h = login(client, name, INIT)
    assert client.get("/api/v1/auth/me", headers=h).status_code == 200
    client.patch(f"/api/v1/system/users/{uid}", headers=admin, json={"enabled": False})
    assert client.get("/api/v1/auth/me", headers=h).status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": name, "password": INIT}).status_code == 401


def test_admin_guards(client, admin):
    me = client.get("/api/v1/auth/me", headers=admin).json()["data"]
    assert client.patch(f"/api/v1/system/users/{me['id']}", headers=admin, json={"enabled": False}).status_code == 400
    demote = client.patch(f"/api/v1/system/users/{me['id']}", headers=admin,
                          json={"role_id": _role_id(client, admin, "viewer")})
    assert demote.status_code == 400 and "平台管理员" in demote.json()["message"]
    viewer = _role_id(client, admin, "viewer")
    dup = client.post("/api/v1/system/users", headers=admin, json={
        "username": "admin", "display_name": "x", "password": INIT, "role_id": viewer})
    assert dup.status_code == 409
    no_role = client.post("/api/v1/system/users", headers=admin, json={
        "username": "user-x", "display_name": "x", "password": INIT, "role_id": 9999})
    assert no_role.status_code == 400
    _, name = _new_user(client, admin, role="admin")
    assert client.get("/api/v1/system/users", headers=login(client, name, INIT)).status_code == 200


def test_user_tenant_scope(client, admin, tenant_ids):
    uid, name = _new_user(client, admin, tenant_ids=[tenant_ids["tenant-b"]])
    me = client.get("/api/v1/auth/me", headers=login(client, name, INIT)).json()["data"]
    assert me["tenant_ids"] == [tenant_ids["tenant-b"]] and [t["code"] for t in me["tenants"]] == ["tenant-b"]
    assert client.patch(f"/api/v1/system/users/{uid}", headers=admin, json={"tenant_ids": []}).status_code == 400
    assert client.patch(f"/api/v1/system/users/{uid}", headers=admin, json={"tenant_ids": [999]}).status_code == 400
    r = client.patch(f"/api/v1/system/users/{uid}", headers=admin, json={"all_tenants": True})
    assert r.status_code == 200 and r.json()["data"]["tenant_ids"] is None


def test_login_options_demo_only_outside_prod(client, monkeypatch):
    """登录页演示账号只在非 prod + 演示数据时下发；生产环境不返回任何账号提示。"""
    from app.core.config import get_settings

    r = client.get("/api/v1/auth/login-options").json()["data"]
    assert r["version"]
    s = get_settings()
    if s.demo_seed and s.env != "prod":
        assert all({"username", "password", "role"} <= set(a) for a in r["demo_accounts"])
    monkeypatch.setattr(s, "env", "prod")
    r = client.get("/api/v1/auth/login-options").json()["data"]
    assert r["demo_accounts"] == [] and r["env"] is None
