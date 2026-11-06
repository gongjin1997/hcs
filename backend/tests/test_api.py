"""基础接口：健康检查、登录、总览（含租户卡片 / ECS 状态 / 容器事件）、告警列表与认领、角色权限、资源与规则、邮件预览。"""
from .conftest import demo_login


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_login_failed(client):
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong-password"})
    assert r.status_code == 401 and r.json()["code"] == 40101


def test_unauthorized(client):
    r = client.get("/api/v1/alarms")
    assert r.status_code == 401 and r.json()["code"] == 40101


def test_dashboard(client, admin, tenant_ids):
    data = client.get("/api/v1/dashboard/summary", headers=admin).json()["data"]
    assert 0 <= data["health_score"] <= 100
    assert [g["code"] for g in data["groups"]] == ["ecs", "dgc_realtime", "dgc_batch", "mrs", "gaussdb", "dcs", "cce"]
    assert len(data["trend"]["hours"]) == 24
    # 规则/证据修复会改变合成告警数；总览须与同范围告警清单一致，不能固定旧误报数量。
    active = client.get("/api/v1/alarms", params={"status_group": "active", "page_size": 200}, headers=admin).json()["data"]
    assert data["kpis"]["active_alarms"] == active["total"]
    assert data["kpis"]["p1"] == sum(row["severity"] == "P1" for row in active["items"])
    assert data["kpis"]["resources"] == 97
    assert data["ledger"]["total"] == 183 and len(data["ledger"]["by_category"]) == 5
    tenants = {t["code"]: t for t in data["tenants"]}
    assert set(tenants) == {"tenant-a", "tenant-b"}
    assert (tenants["tenant-a"]["auth_type"], tenants["tenant-a"]["metrics_source"]) == ("PASSWORD", "AUTO")
    # 演示 AUTO 只选已配置的本地合成 Prometheus；生产数据源配置不受演示初始化影响。
    assert tenants["tenant-a"]["metrics_effective"] == "PROMETHEUS"
    assert (tenants["tenant-b"]["auth_type"], tenants["tenant-b"]["metrics_source"]) == ("AKSK", "PROMETHEUS")
    assert [sum(e["count"] for e in tenants[c]["ecs"]) for c in ("tenant-a", "tenant-b")] == [34, 15]
    assert data["ecs"]["total"] == 49 and {s["status"] for s in data["ecs"]["by_status"]} >= {"ACTIVE", "SHUTOFF", "ERROR"}
    # 演示历史按业务高峰分布，24h 窗口内的榜首会随测试时间变化。
    top = data["pods"]["top_restart"]
    assert data["pods"]["restarts_24h"] > 0 and top
    assert all(item["workload"] and item["count"] > 0 for item in top)
    assert [item["count"] for item in top] == sorted((item["count"] for item in top), reverse=True)
    assert sum(item["count"] for item in top) <= data["pods"]["restarts_24h"]
    # 顶部切换到租户A：只统计租户A 的数据
    a = client.get("/api/v1/dashboard/summary", headers={**admin, "X-Tenant-Id": str(tenant_ids["tenant-a"])}).json()
    assert a["data"]["ledger"]["total"] == 129 and a["data"]["kpis"]["active_alarms"] == 20   # 含 1 条平台告警
    assert [t["code"] for t in a["data"]["tenants"]] == ["tenant-a"]


def test_alarm_list_and_ack(client, admin):
    page = client.get("/api/v1/alarms", params={"severity": ["P1"]}, headers=admin).json()["data"]
    assert page["total"] == 3 and all(a["severity"] == "P1" for a in page["items"])
    firing = next(a for a in page["items"] if a["status"] == "FIRING")
    r = client.post(f"/api/v1/alarms/{firing['id']}/ack", json={"note": "处理中"}, headers=admin)
    assert r.json()["data"]["status"] == "ACKED"
    r = client.post(f"/api/v1/alarms/{firing['id']}/ack", headers=admin)
    assert r.status_code == 409 and r.json()["code"] == 40901
    detail = client.get(f"/api/v1/alarms/{firing['id']}", headers=admin).json()["data"]
    assert [x["action"] for x in detail["logs"]][-1] == "ACK" and detail["runbook"]


def test_oncall_can_operate_but_not_manage(client):
    oncall = demo_login(client, "oncall")
    me = client.get("/api/v1/auth/me", headers=oncall).json()["data"]
    assert (me["role_code"], me["role_name"], me["is_admin"]) == ("oncall", "值班运维", False)
    assert "alarm:handle" in me["permissions"] and "user:manage" not in me["permissions"]
    assert me["tenant_ids"] is None and len(me["tenants"]) == 2
    p2 = client.get("/api/v1/alarms", params={"severity": ["P2"]}, headers=oncall).json()["data"]["items"]
    firing = next(a for a in p2 if a["status"] == "FIRING" and a["resource_name"] != "redis-prod-01")
    r = client.post(f"/api/v1/alarms/{firing['id']}/ack", headers=oncall)
    assert r.status_code == 200 and r.json()["data"]["status"] == "ACKED"
    forbidden = (
        ("get", "/api/v1/system/users", None),
        ("post", "/api/v1/system/users", {"username": "x01", "display_name": "x", "role_id": 1, "password": "Init-pass-01"}),
        ("put", "/api/v1/rules/R-DCS-005", {"name": "x"}),
        ("get", "/api/v1/system/roles", None),
        ("get", "/api/v1/system/audit-logs", None),
        ("delete", "/api/v1/system/tenants/1", None),
    )
    for method, url, body in forbidden:
        r = client.request(method.upper(), url, json=body, headers=oncall)
        assert r.status_code == 403 and r.json()["code"] == 40301, url


def test_resources_and_rules(client, admin):
    meta = client.get("/api/v1/resources/types", headers=admin).json()["data"]
    assert set(meta) == {"categories", "types", "env_types", "options", "dict", "classifications"}
    assert all(t["collect"]["enabled"] for t in meta["types"])
    assert "电商交易" in meta["options"]["business"] and "张伟" in meta["options"]["owner"]
    assert {t["code"] for t in meta["types"]} >= {"ecs_server", "dcs_instance", "dgc_realtime_job", "cce_workload"}
    page = client.get("/api/v1/resources", params={"resource_type": "dcs_instance"}, headers=admin).json()["data"]
    assert page["items"][0]["name"] == "redis-prod-01" and page["items"][0]["active_alarms"] == 1
    rules = client.get("/api/v1/rules", headers=admin).json()["data"]
    assert any(r["id"] == "R-DCS-005" for r in rules)
    expr = next(r["expr"] for r in rules if r["id"] == "R-DCS-005")
    dry = client.post("/api/v1/rules/dry-run", headers=admin, json={
        "kind": "threshold", "expr": expr, "recover": {"below": 75}, "facts": {"memory_usage": 92.3}}).json()["data"]
    assert dry == {"matched": True, "severity": "P2", "recovered": False, "message": "内存使用率 92.3%（≥90%）"}
    bad = client.post("/api/v1/rules/dry-run", headers=admin, json={
        "kind": "state", "expr": {"when": "__import__('os')", "severity": "P1"}, "facts": {}})
    assert bad.status_code == 400 and bad.json()["code"] == 40001


def test_mail_preview(client, admin):
    page = client.get("/api/v1/alarms", params={"keyword": "redis-prod-01"}, headers=admin).json()["data"]
    data = client.get(f"/api/v1/notify/preview/{page['items'][0]['id']}", headers=admin).json()["data"]
    assert data["subject"] == "【P2-重要】【租户A】【DCS】redis-prod-01 内存使用率 92.3%（≥90%）"
    assert "redis-prod-01" in data["html"]
    assert set(data["recipients"]) == {"zhangwei@example.com", "lina@example.com", "zhoumin@example.com"}
    contacts = client.get("/api/v1/notify/contacts", headers=admin).json()["data"]
    disabled = next(c for c in contacts if c["username"] == "chenjing")
    assert disabled["user_id"] and disabled["enabled"] is False
    assert disabled["email"] not in data["recipients"]
