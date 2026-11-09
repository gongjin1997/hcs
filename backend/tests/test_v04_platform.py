"""v0.4 平台运维类功能：通知配置维护（联系人 / 联系组 / 路由）、路由模拟、测试邮件、平台自身告警（R-PLAT-001 ~ 004）、
平台状态接口、/metrics、数据清理、SSE 一次性票据、迁移 0003 的权限补齐。"""
import json
from datetime import timedelta

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select, text

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.deps import user_from_ticket
from app.core.permissions import ADDED_IN
from app.core.response import BizError
from app.models import (
    AlarmEvent,
    AlarmEventLog,
    AuditLog,
    Contact,
    ContactGroup,
    InspectionRun,
    Notification,
    NotifyRoute,
    Silence,
    User,
    WorkerHeartbeat,
)
from app.models.base import utcnow
from app.notify.dispatcher import dispatch_pending
from app.services import platform_watch, settings_service
from app.services.cleanup import run_cleanup

from .conftest import demo_login
from .test_migrations import _cfg

API = "/api/v1/notify"


# ---------------------------------------------------------------- 通知配置维护
def test_notify_contact_group_route_crud(client, admin):
    person = {"username": "tmp-contact-person", "display_name": "临时联系人", "email": "tmp.contact@example.com",
              "login_enabled": False}
    bad = client.post("/api/v1/system/users", json={**person, "email": "not-an-email"}, headers=admin)
    assert bad.status_code == 400
    dup = client.post("/api/v1/system/users", json={**person, "email": "zhangwei@example.com"}, headers=admin)
    assert dup.status_code == 409 and "邮箱" in dup.json()["message"] and "用户管理" in dup.json()["message"]
    created = client.post("/api/v1/system/users", json=person, headers=admin)
    assert created.status_code == 200, created.text
    user = created.json()["data"]
    uid, cid = user["id"], user["contact_id"]
    assert cid and user["login_enabled"] is False and user["role_id"] is None
    contact = next(c for c in client.get(f"{API}/contacts", headers=admin).json()["data"] if c["id"] == cid)
    assert contact["user_id"] == uid and contact["username"] == person["username"]
    assert contact["name"] == person["display_name"] and contact["enabled"] is True
    legacy = {"name": "绕过统一入口", "email": "legacy.contact@example.com"}
    for method, path, payload in (("POST", "/contacts", legacy), ("PUT", f"/contacts/{cid}", legacy),
                                   ("DELETE", f"/contacts/{cid}", None)):
        moved = client.request(method, f"{API}{path}", json=payload, headers=admin)
        assert moved.status_code == 409 and "用户管理" in moved.json()["message"]

    body = {"code": "tmp-group", "name": "临时联系组", "member_ids": [cid], "leader_ids": [cid]}
    gid = client.post(f"{API}/groups", json=body, headers=admin).json()["data"]["id"]
    assert client.post(f"{API}/groups", json=body, headers=admin).status_code == 409
    assert client.post(f"{API}/groups", json={**body, "code": "Bad Code"}, headers=admin).status_code == 400
    assert client.post(f"{API}/groups", json={**body, "code": "tmp-empty", "member_ids": []},
                       headers=admin).status_code == 400
    unregistered = client.post(f"{API}/groups", json={**body, "code": "tmp-unknown", "member_ids": [999999]},
                                headers=admin)
    assert unregistered.status_code == 400 and "用户管理" in unregistered.json()["message"]
    r = client.put(f"{API}/groups/{gid}", json={**body, "code": "tmp-renamed"}, headers=admin)
    assert r.status_code == 400 and "不能修改" in r.json()["message"]

    route = {"name": "临时路由", "priority": 50, "match": {"services": ["dcs"], "severities": ["P1"]},
             "group_code": "tmp-group", "escalation": [{"after_min": 30, "to": "leader", "severities": ["P1"]}]}
    assert client.post(f"{API}/routes", json={**route, "match": {"services": ["nope"]}},
                       headers=admin).status_code == 400
    assert client.post(f"{API}/routes", json={**route, "group_code": "no-such"}, headers=admin).status_code == 400
    assert client.post(f"{API}/routes", json={**route, "match": {"tenant_ids": [9999]}},
                       headers=admin).status_code == 400
    rid = client.post(f"{API}/routes", json=route, headers=admin).json()["data"]["id"]
    got = next(x for x in client.get(f"{API}/routes", headers=admin).json()["data"] if x["id"] == rid)
    assert got["group_name"] == "临时联系组" and got["member_count"] == 1 and got["match"]["services"] == ["dcs"]
    groups = {g["code"]: g for g in client.get(f"{API}/groups", headers=admin).json()["data"]}
    assert groups["tmp-group"]["routes"] == ["临时路由"] and groups["tmp-group"]["leaders"][0]["id"] == cid

    # 仍被路由使用的联系组不能删除；人员由统一用户入口维护，联系人 ID 和联系组引用保持稳定。
    r = client.delete(f"{API}/groups/{gid}", headers=admin)
    assert r.status_code == 409 and r.json()["data"]["routes"] == ["临时路由"]
    updated = client.patch(f"/api/v1/system/users/{uid}", json={"display_name": "临时人员（更新）",
                           "email": "tmp.contact.updated@example.com", "phone": "13800001000"}, headers=admin)
    assert updated.status_code == 200 and updated.json()["data"]["contact_id"] == cid
    assert updated.json()["data"]["groups"] == ["临时联系组"]
    contact = next(c for c in client.get(f"{API}/contacts", headers=admin).json()["data"] if c["id"] == cid)
    assert (contact["user_id"], contact["name"], contact["email"], contact["phone"], contact["enabled"]) == (
        uid, "临时人员（更新）", "tmp.contact.updated@example.com", "13800001000", True)
    group = next(g for g in client.get(f"{API}/groups", headers=admin).json()["data"] if g["id"] == gid)
    assert group["member_ids"] == group["leader_ids"] == [cid]
    assert group["members"][0]["name"] == "临时人员（更新）"

    # 修改路由 → 审计记录变更前后
    assert client.put(f"{API}/routes/{rid}", json={**route, "enabled": False}, headers=admin).status_code == 200
    logs = client.get("/api/v1/system/audit-logs", params={"action": "notify.route.update"},
                      headers=admin).json()["data"]["items"]
    assert logs[0]["detail"]["changes"] == {"enabled": [True, False]}

    # 排序：必须包含全部路由；按传入顺序重排为 10、20、30 …（相对顺序不变时匹配结果不变）
    rows = client.get(f"{API}/routes", headers=admin).json()["data"]
    before = {x["id"]: x["priority"] for x in rows}
    assert client.post(f"{API}/routes/reorder", json={"ids": [x["id"] for x in rows][1:]},
                       headers=admin).status_code == 400
    assert client.post(f"{API}/routes/reorder", json={"ids": [x["id"] for x in rows]}, headers=admin).status_code == 200
    after = [x["priority"] for x in client.get(f"{API}/routes", headers=admin).json()["data"]]
    assert after == [10 * i for i in range(1, len(rows) + 1)]
    with SessionLocal() as db:                          # 恢复演示数据的优先级，避免影响后续用例
        for r_ in db.scalars(select(NotifyRoute)):
            r_.priority = before[r_.id]
        db.commit()

    assert client.delete(f"{API}/routes/{rid}", headers=admin).status_code == 200
    assert client.delete(f"{API}/groups/{gid}", headers=admin).status_code == 200
    disabled = client.patch(f"/api/v1/system/users/{uid}", json={"enabled": False}, headers=admin)
    assert disabled.status_code == 200 and disabled.json()["data"]["enabled"] is False
    assert disabled.json()["data"]["contact_id"] == cid
    contact = next(c for c in client.get(f"{API}/contacts", headers=admin).json()["data"] if c["id"] == cid)
    assert contact["user_id"] == uid and contact["enabled"] is False and contact["groups"] == []
    logs = client.get("/api/v1/system/audit-logs", params={"target_type": "user", "page_size": 200},
                      headers=admin).json()["data"]["items"]
    actions = {x["action"] for x in logs if x["target_id"] == str(uid)}
    assert {"user.create", "user.update"} <= actions
    assert next(x for x in logs if x["target_id"] == str(uid))["detail"]["changes"] == {"启用": [True, False]}


def test_notify_manage_permissions(client):
    oncall = demo_login(client, "oncall")                # 值班运维：可以查看，不能维护
    assert client.get(f"{API}/routes", headers=oncall).status_code == 200
    r = client.post(f"{API}/contacts", json={"name": "x", "email": "x1@example.com"}, headers=oncall)
    assert r.status_code == 403
    person = {"username": "notify-only-person", "display_name": "通知人员", "email": "x2@example.com",
              "login_enabled": False}
    assert client.post("/api/v1/system/users", json=person, headers=oncall).status_code == 403
    lead = demo_login(client, "zhaolei")                 # 通知管理员可维护联系组，但人员维护需要 user:manage。
    permissions = client.get("/api/v1/auth/me", headers=lead).json()["data"]["permissions"]
    assert "notify:manage" in permissions and "user:manage" not in permissions
    assert client.post("/api/v1/system/users", json=person, headers=lead).status_code == 403
    contact = next(c for c in client.get(f"{API}/contacts", headers=lead).json()["data"] if c["enabled"])
    cid = contact["id"]
    assert client.patch(f"/api/v1/system/users/{contact['user_id']}", json={"enabled": False},
                        headers=lead).status_code == 403
    legacy = {"name": person["display_name"], "email": person["email"]}
    for method, path, payload in (("POST", "/contacts", legacy), ("PUT", f"/contacts/{cid}", legacy),
                                   ("DELETE", f"/contacts/{cid}", None)):
        moved = client.request(method, f"{API}{path}", json=payload, headers=lead)
        assert moved.status_code == 409 and "用户管理" in moved.json()["message"]
    unchanged = next(c for c in client.get(f"{API}/contacts", headers=lead).json()["data"] if c["id"] == cid)
    assert unchanged == contact
    body = {"code": "tmp-notify-permissions", "name": "通知权限测试组", "member_ids": [cid], "leader_ids": [cid]}
    assert client.post(f"{API}/groups", json=body, headers=oncall).status_code == 403
    created = client.post(f"{API}/groups", json=body, headers=lead)
    assert created.status_code == 200, created.text
    gid = created.json()["data"]["id"]
    assert client.put(f"{API}/groups/{gid}", json={**body, "name": "已更新联系组"}, headers=lead).status_code == 200
    assert client.delete(f"{API}/groups/{gid}", headers=lead).status_code == 200
    viewer = demo_login(client, "viewer")
    assert client.post(f"{API}/test-mail", json={"to": ["a@example.com"]}, headers=viewer).status_code == 403


def test_notify_simulate(client, admin, tenant_ids):
    r = client.post(f"{API}/simulate", json={"tenant_id": tenant_ids["tenant-b"], "service": "dcs", "severity": "P1"},
                    headers=admin).json()["data"]
    matched = [s["name"] for s in r["steps"] if s["matched"]]
    assert matched == ["P1 紧急 → 运维负责人（抄送）", "租户B 告警 → 互联网运维组"]
    assert r["recipients"] == ["zhaolei@example.com", "wangfang@example.com", "liuqiang@example.com"]
    assert all(s["skipped"] for s in r["steps"] if s["priority"] > 10)           # 不抄送的路由命中后停止匹配
    assert r["escalation"] and r["silence"] is None

    plat = client.post(f"{API}/simulate", json={"service": "platform", "resource_type": "platform_task",
                                                "severity": "P2"}, headers=admin).json()["data"]
    assert [s["name"] for s in plat["steps"] if s["matched"]] == ["平台自身告警 → 运维负责人"]
    assert plat["recipients"] == ["zhaolei@example.com"] and plat["platform_fallback"] is None
    assert client.post(f"{API}/simulate", json={"service": "dcs", "severity": "P9"}, headers=admin).status_code == 400


def test_test_mail(client, admin, monkeypatch):
    sent = []
    monkeypatch.setattr("app.api.v1.notify.send", lambda msg: sent.append(msg))
    ids = []
    for username, email in (("smtp-ops-user", "ops@example.com"), ("smtp-dba-user", "dba@example.com")):
        response = client.post("/api/v1/system/users", json={
            "username": username, "display_name": username, "email": email, "login_enabled": False}, headers=admin)
        if response.status_code == 409:
            person = next(u for u in client.get("/api/v1/system/users", headers=admin).json()["data"] if u["email"] == email)
        else:
            assert response.status_code == 200, response.text
            person = response.json()["data"]
        ids.append(person["id"])
    r = client.post(f"{API}/test-mail", json={"user_ids": ids}, headers=admin)
    assert r.status_code == 200 and r.json()["data"]["sent_to"] == ["ops@example.com", "dba@example.com"]
    assert "测试邮件" in sent[0]["Subject"] and "ops@example.com" in sent[0]["To"]
    assert client.post(f"{API}/test-mail", json={"to": ["bad"]}, headers=admin).status_code == 400
    assert client.post(f"{API}/test-mail", json={"to": []}, headers=admin).status_code == 400

    def boom(_msg):
        raise ConnectionRefusedError("Connection refused")

    monkeypatch.setattr("app.api.v1.notify.send", boom)
    r = client.post(f"{API}/test-mail", json={"to": ["ops@example.com"]}, headers=admin)
    assert r.status_code == 502 and "Connection refused" in r.json()["message"]
    log = client.get("/api/v1/system/audit-logs", params={"action": "notify.test_mail"},
                     headers=admin).json()["data"]["items"][0]
    assert log["result"] == "FAILED" and "Connection refused" in log["detail"]["error"]
    info = client.get(f"{API}/smtp", headers=admin).json()["data"]
    assert "password" not in info and {"host", "port", "sender"} <= set(info)


# ---------------------------------------------------------------- 平台自身告警
def _setup_route(db) -> None:
    user = User(username="platform-recipient", display_name="赵磊", email="zhaolei@example.com",
                password_hash="!", login_enabled=False)
    db.add(user)
    db.flush()
    db.add(Contact(id=901, user_id=user.id, name=user.display_name, email=user.email))
    db.add(ContactGroup(code="leaders", name="运维负责人", member_ids=[901], leader_ids=[901]))
    db.add(NotifyRoute(name="平台自身告警", priority=1, match={"services": ["platform"]}, group_code="leaders"))
    db.commit()


def _run(db, status: str, minutes_ago: int, tenant_id: int, error: str | None = None,
         task: str = "mrs_job_status") -> InspectionRun:
    now = utcnow()
    r = InspectionRun(task_code=task, tenant_id=tenant_id, project_ref=101, status=status, error_summary=error,
                      started_at=now - timedelta(minutes=minutes_ago), finished_at=now - timedelta(minutes=minutes_ago))
    db.add(r)
    db.commit()
    platform_watch.on_run(db, r)
    return r


def _platform_alarms(db, rule_id: str) -> list[AlarmEvent]:
    return list(db.scalars(select(AlarmEvent).where(AlarmEvent.rule_id == rule_id).order_by(AlarmEvent.id)))


def test_platform_task_failures_and_auth(world, mem_db):
    _setup_route(mem_db)
    a = world["tenant-a"].id
    _run(mem_db, "SUCCESS", 30, a)
    _run(mem_db, "FAILED", 20, a, "[504] APIGW.0201: Backend timeout")
    _run(mem_db, "FAILED", 15, a, "[504] APIGW.0201: Backend timeout")
    assert _platform_alarms(mem_db, "R-PLAT-001") == []                    # 连续 2 次：未达阈值（默认 3）
    _run(mem_db, "FAILED", 10, a, "[504] APIGW.0201: Backend timeout")
    [al] = _platform_alarms(mem_db, "R-PLAT-001")
    assert al.status == "FIRING" and al.service == "platform" and al.tenant_id == a and al.severity == "P2"
    assert al.title.startswith("连续 3 次执行未成功") and al.detail["tenant"] == world["tenant-a"].short_name
    n = mem_db.scalar(select(Notification).where(Notification.alarm_id == al.id))
    assert n.kind == "FIRING" and n.recipients == ["zhaolei@example.com"]
    _run(mem_db, "FAILED", 5, a, "[504] APIGW.0201: Backend timeout")      # 继续失败：刷新同一条告警
    assert len(_platform_alarms(mem_db, "R-PLAT-001")) == 1 and al.count == 2
    _run(mem_db, "SUCCESS", 1, a)
    assert al.status == "RESOLVED" and al.resolved_at is not None
    assert mem_db.scalar(select(Notification).where(Notification.alarm_id == al.id, Notification.kind == "RESOLVED"))

    # 认证 / 权限失败：首次即告警；同任务、同租户、同项目真实成功后恢复
    _run(mem_db, "FAILED", 3, a, "[401] IAM.0101: The username or password is wrong", task="dcs_status")
    [auth] = _platform_alarms(mem_db, "R-PLAT-002")
    assert auth.status == "FIRING" and auth.resource_name == "HCS 接口认证" and "认证失败" in auth.title
    _run(mem_db, "SUCCESS", 1, a, task="dcs_status")
    assert auth.status == "RESOLVED"

    # 关闭平台自身告警后不再产生
    settings_service.save(mem_db, "platform_alarm", {"enabled": False}, operator="test")
    mem_db.commit()
    for m in (3, 2, 1):
        _run(mem_db, "FAILED", m, a, "[500] boom", task="gaussdb_status")
    assert not [x for x in _platform_alarms(mem_db, "R-PLAT-001") if x.status == "FIRING"]


def test_platform_notify_failed_and_heartbeat(world, mem_db):
    _setup_route(mem_db)
    mem_db.add(User(username="delivery-test", display_name="通知接收人", email="ops@example.com",
                    password_hash="!", login_enabled=False, notify_enabled=True))
    mem_db.commit()
    now = utcnow()
    alarm = AlarmEvent(fingerprint="fp-dcs-1", rule_id="R-DCS-001", tenant_id=world["tenant-a"].id, service="dcs",
                       resource_type="dcs_instance", resource_name="redis-prod-01", sub_object="", severity="P2",
                       status="FIRING", title="内存使用率过高", check_item="memory", first_seen=now, last_seen=now,
                       detail={})
    mem_db.add(alarm)
    mem_db.flush()
    mem_db.add(Notification(alarm_id=alarm.id, kind="FIRING", recipients=["ops@example.com"], status="PENDING",
                            created_at=now))
    mem_db.commit()

    def refuse(_msg):
        raise ConnectionRefusedError("[Errno 111] Connection refused")

    for i in range(5):                                   # 退避重试 5 次后置为 FAILED → R-PLAT-003
        dispatch_pending(mem_db, sender=refuse, now=now + timedelta(hours=2 * i))
    failed = mem_db.scalar(select(Notification).where(Notification.alarm_id == alarm.id))
    assert failed.status == "FAILED" and failed.retry == 5
    [al] = _platform_alarms(mem_db, "R-PLAT-003")
    assert al.status == "FIRING" and al.resource_name == "告警通知（SMTP）" and "重试 5 次" in al.title
    assert al.detail["notification_id"] == failed.id

    sent = []
    dispatch_pending(mem_db, sender=sent.append, now=now + timedelta(hours=12))
    assert sent and al.status == "RESOLVED"              # 之后有通知发送成功 → 自动恢复

    # 心跳：从未启动的进程不告警；超时告警；恢复后自动恢复
    assert platform_watch.check_heartbeats(mem_db, now=now) == []
    platform_watch.beat(mem_db, "scheduler", {"jobs": 20})
    hb = mem_db.get(WorkerHeartbeat, "scheduler")
    assert hb.version and hb.info == {"jobs": 20}
    assert platform_watch.check_heartbeats(mem_db, now=hb.beat_at + timedelta(minutes=1)) == []
    assert platform_watch.check_heartbeats(mem_db, now=hb.beat_at + timedelta(minutes=10)) == ["scheduler"]
    [late] = _platform_alarms(mem_db, "R-PLAT-004")
    assert late.status == "FIRING" and "10 分钟没有心跳" in late.title and late.resource_name.startswith("调度进程")
    platform_watch.check_heartbeats(mem_db, now=hb.beat_at + timedelta(minutes=2))
    assert late.status == "RESOLVED"


def test_platform_status_api(client, admin, tenant_ids):
    # 此场景验证已启用任务的失败聚合；新库的 MRS 原生任务默认关闭。
    assert client.put("/api/v1/system/tasks/mrs_job_status", headers=admin, json={"enabled": True}).status_code == 200
    d = client.get("/api/v1/system/platform", headers=admin).json()["data"]
    assert d["version"] == "0.4.0" and {w["name"] for w in d["workers"]} == {"scheduler", "notifier"}
    assert {"sent_24h", "failed_24h", "pending", "retrying", "last_sent_at"} <= set(d["outbox"])
    mrs = next(t for t in d["tasks"] if t["code"] == "mrs_job_status")
    a = next(p for p in mrs["tenants"] if p["tenant_id"] == tenant_ids["tenant-a"] and p["project_ref"] is None)
    assert mrs["status"] == "FAILED" and a["fails"] >= d["task_fail_threshold"] and a["last_status"] == "FAILED"
    assert a["project_ref"] is None and a["project_scope"] == "UNKNOWN"  # seed 历史不能推断到真实项目
    assert any(x["rule_id"] == "R-PLAT-001" for x in d["platform_alarms"])
    assert d["cleanup"]["status"] == "SUCCESS" and d["cleanup"]["stats"]
    assert d["active_silences"] >= 2 and d["metrics"]["path"] == "/metrics"
    assert d["platform_alarm"]["task_fail_threshold"] == 3
    wangfang = demo_login(client, "wangfang")            # 只看租户B：看不到租户A 的平台告警和运行
    d = client.get("/api/v1/system/platform", headers=wangfang).json()["data"]
    assert all(x["tenant_id"] in (None, tenant_ids["tenant-b"]) for x in d["platform_alarms"])
    mrs = next(t for t in d["tasks"] if t["code"] == "mrs_job_status")
    assert all(p["tenant_id"] in (None, tenant_ids["tenant-b"]) for p in mrs["tenants"])


def test_metrics_endpoint(client, monkeypatch):
    r = client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "hcs_inspector_alarms_active" in r.text and "hcs_inspector_http_request_duration_seconds_count" in r.text
    monkeypatch.setattr(get_settings(), "metrics_token", "metrics-token-for-test")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer metrics-token-for-test"}).status_code == 200


# ---------------------------------------------------------------- 数据清理
def test_cleanup_keeps_active_data(world, mem_db):
    now = utcnow()
    old, recent = now - timedelta(days=400), now - timedelta(days=1)
    a = world["tenant-a"].id
    for at in (old, recent):
        mem_db.add(InspectionRun(task_code="ecs_status", tenant_id=a, status="SUCCESS", started_at=at, finished_at=at))
        mem_db.add(AuditLog(at=at, operator="test", action="alarm.ack"))
    mem_db.add(InspectionRun(task_code="ecs_status", tenant_id=a, status="RUNNING", started_at=old))
    alarms = {}
    for st in ("RESOLVED", "FIRING", "SUPPRESSED"):
        al = AlarmEvent(fingerprint=f"fp-{st}", rule_id="R-ECS-001", tenant_id=a, service="ecs",
                        resource_type="ecs_server", resource_name="ecs-01", sub_object="", severity="P3", status=st,
                        title=st, check_item="status", first_seen=old, last_seen=old, detail={})
        mem_db.add(al)
        mem_db.flush()
        mem_db.add(AlarmEventLog(alarm_id=al.id, action="FIRE", to_status="FIRING", operator="system", at=old))
        mem_db.add(Notification(alarm_id=al.id, kind="FIRING", recipients=["x@example.com"], status="SENT",
                                created_at=old if st == "RESOLVED" else recent))
        alarms[st] = al
    mem_db.add(Silence(name="旧维护窗口", kind="MAINTENANCE", matchers={}, starts_at=old, ends_at=old + timedelta(hours=2)))
    mem_db.add(Silence(name="新维护窗口", kind="MAINTENANCE", matchers={}, starts_at=recent, ends_at=now))
    mem_db.commit()

    stats = run_cleanup(mem_db, now=now)
    assert stats["inspection_run"] == 1 and stats["audit_log"] == 1 and stats["alarm_event"] == 1
    assert stats["alarm_event_log"] == 1 and stats["alarm_silence"] == 1
    left = {x.status for x in mem_db.scalars(select(AlarmEvent))}
    assert left == {"FIRING", "SUPPRESSED"}                     # 活动告警不删除（即使很久以前产生）
    assert mem_db.scalar(select(InspectionRun).where(InspectionRun.status == "RUNNING")) is not None
    assert {s.name for s in mem_db.scalars(select(Silence))} == {"新维护窗口"}
    assert run_cleanup(mem_db, now=now)["inspection_run"] == 0   # 幂等


# ---------------------------------------------------------------- SSE 一次性票据
def test_sse_ticket(client, admin):
    r = client.post("/api/v1/stream/ticket", headers=admin).json()["data"]
    assert r["expires_in"] == 60
    ticket = r["ticket"]
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {ticket}"}).status_code == 401
    access = admin["Authorization"].removeprefix("Bearer ")
    assert client.get("/api/v1/stream/alarms", params={"ticket": access}).status_code == 401   # 登录令牌不能当票据
    assert client.get("/api/v1/stream/alarms", params={"ticket": "garbage"}).status_code == 401
    assert client.get("/api/v1/stream/alarms").status_code == 400                               # 缺少票据
    assert user_from_ticket(ticket).username == "admin"
    with pytest.raises(BizError) as e:                                                          # 只能使用一次
        user_from_ticket(ticket)
    assert e.value.http_status == 401 and "已使用" in e.value.message
    assert client.post("/api/v1/stream/ticket").status_code == 401


# ---------------------------------------------------------------- 迁移 0003：内置角色补齐权限
def test_migration_0003_grants_new_permissions(tmp_path):
    url = f"sqlite:///{tmp_path / 'mig3.db'}"
    cfg = _cfg(url)
    command.upgrade(cfg, "0002")
    engine = create_engine(url)
    old = {"ops_lead": ["alarm:view", "alarm:handle", "report:view"], "oncall": ["alarm:view", "alarm:handle"]}
    with engine.begin() as c:                            # 模拟 v0.3 的内置角色（没有 v0.4 新增的权限点）+ 一个自定义角色
        for code, perms in old.items():
            c.execute(text("UPDATE sys_role SET permissions = :p WHERE code = :c"),
                      {"p": json.dumps(perms), "c": code})
        c.execute(text("INSERT INTO sys_role (code, name, description, permissions, builtin, created_at, updated_at)"
                       " VALUES ('custom', '自定义', '', '[\"alarm:view\"]', 0, '2026-10-01', '2026-10-01')"))
    command.upgrade(cfg, "head")
    assert {"alarm_silence", "collect_switch", "sys_setting", "worker_heartbeat"} <= set(inspect(engine).get_table_names())
    with engine.connect() as c:
        perms = {code: json.loads(p) for code, p in c.execute(text("SELECT code, permissions FROM sys_role"))}
    assert perms["ops_lead"] == old["ops_lead"] + ADDED_IN["0.4.0"]["ops_lead"]
    assert perms["oncall"] == old["oncall"] + ["silence:manage"]
    assert perms["custom"] == ["alarm:view"] and perms["admin"] == ["*"]     # 自定义角色不自动修改
    command.downgrade(cfg, "0002")
    assert "alarm_silence" not in inspect(engine).get_table_names()
    engine.dispose()
