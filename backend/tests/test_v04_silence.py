"""v0.4 静默 / 维护窗口：列表与状态、新建即抑制、取消后补发通知、快速静默、预览、校验、每周窗口（跨零点）、
到期自动释放、数据范围（租户用户只能看到 / 修改本租户的静默）。"""
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import AlarmEvent, AlarmEventLog, Notification, Silence
from app.models.base import utcnow
from app.services import silence_service

from .conftest import demo_login


def _alarm(db, name: str, rule: str) -> AlarmEvent:
    return db.scalar(select(AlarmEvent).where(AlarmEvent.resource_name == name, AlarmEvent.rule_id == rule))


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_seeded_silences(client, admin):
    data = client.get("/api/v1/silences", params={"status": "all"}, headers=admin).json()["data"]
    assert data["counts"] == {"ACTIVE": 2, "WAITING": 1, "PENDING": 1, "EXPIRED": 1, "CANCELLED": 1}
    items = {s["name"]: s for s in data["items"]}
    rec = next(s for s in data["items"] if s["recurrence"])
    assert rec["status"] == "WAITING" and rec["next_window"] and rec["recurrence"]["start"] == "22:00"
    assert "级别：P3、P4" in rec["describe"]
    quick = next(s for n, s in items.items() if n.startswith("静默告警：ecs-report-01"))
    assert quick["status"] == "ACTIVE" and quick["suppressed_now"] == 1 and quick["created_by"] == "张伟"
    current = client.get("/api/v1/silences", headers=admin).json()["data"]["items"]
    assert {s["status"] for s in current} == {"ACTIVE", "WAITING", "PENDING"}
    alarms = client.get("/api/v1/alarms", params={"status_group": "suppressed"}, headers=admin).json()["data"]["items"]
    assert {a["resource_name"] for a in alarms} >= {"ecs-report-01", "ecs-batch-01"}
    assert all(a["silence_id"] for a in alarms if a["resource_name"] in ("ecs-report-01", "ecs-batch-01"))


def test_silence_scope(client, tenant_ids):
    lina = demo_login(client, "lina")                       # 只有租户A
    names = [s["name"] for s in client.get("/api/v1/silences", params={"status": "all"},
                                           headers=lina).json()["data"]["items"]]
    assert names and not any("租户B" in n or "ecs-batch-01" in n for n in names)
    end = _iso(utcnow() + timedelta(hours=1))
    body = {"name": "越权", "matchers": {"tenant_ids": [tenant_ids["tenant-b"]]}, "ends_at": end}
    assert client.post("/api/v1/silences", json=body, headers=lina).status_code == 403
    body = {"name": "不带租户", "matchers": {"services": ["ecs"], "name_contains": "no-such-host"},
            "ends_at": end}                                  # 租户用户的静默自动限定在本租户
    r = client.post("/api/v1/silences", json=body, headers=lina)
    assert r.status_code == 200 and r.json()["data"]["matchers"]["tenant_ids"] == [tenant_ids["tenant-a"]]
    client.post(f"/api/v1/silences/{r.json()['data']['id']}/cancel", headers=lina)
    viewer = demo_login(client, "viewer")
    assert client.post("/api/v1/silences", json=body, headers=viewer).status_code == 403
    assert client.get("/api/v1/silences", headers=viewer).status_code == 200


def test_validation(client, admin):
    now = utcnow()
    cases = [({"name": "空条件", "matchers": {}, "ends_at": _iso(now + timedelta(hours=1))}, "至少需要一个匹配条件"),
             ({"name": "倒序", "matchers": {"services": ["ecs"]}, "ends_at": _iso(now - timedelta(hours=1))}, "晚于"),
             ({"name": "太长", "matchers": {"services": ["ecs"]}, "ends_at": _iso(now + timedelta(days=40))}, "31 天"),
             ({"name": "坏星期", "matchers": {"services": ["ecs"]}, "ends_at": _iso(now + timedelta(days=40)),
               "recurrence": {"weekdays": [8], "start": "22:00", "end": "02:00"}}, "星期"),
             ({"name": "坏时间", "matchers": {"services": ["ecs"]}, "ends_at": _iso(now + timedelta(days=4)),
               "recurrence": {"weekdays": [1], "start": "25:00", "end": "02:00"}}, "HH:MM")]
    for body, msg in cases:
        r = client.post("/api/v1/silences", json=body, headers=admin)
        assert r.status_code == 400 and msg in r.json()["message"], body["name"]


def test_create_suppress_cancel_and_renotify(client, admin, tenant_ids):
    with SessionLocal() as db:
        a = _alarm(db, "ecs-etl-02", "R-ECS-004")
        assert a.status == "FIRING"
        aid, ref = a.id, a.resource_ref
    pre = client.post("/api/v1/silences/preview", json={"matchers": {"resource_refs": [ref]}}, headers=admin)
    assert pre.json()["data"]["total"] >= 1 and pre.json()["data"]["items"][0]["id"] == aid
    body = {"name": "ecs-etl-02 扩容", "kind": "MAINTENANCE", "reason": "测试",
            "matchers": {"tenant_ids": [tenant_ids["tenant-a"]], "resource_refs": [ref]},
            "ends_at": _iso(utcnow() + timedelta(hours=2))}
    r = client.post("/api/v1/silences", json=body, headers=admin)
    assert r.status_code == 200 and r.json()["data"]["suppressed"] == 1
    sid = r.json()["data"]["id"]
    detail = client.get(f"/api/v1/alarms/{aid}", headers=admin).json()["data"]
    assert detail["status"] == "SUPPRESSED" and detail["silence_id"] == sid
    assert detail["suppress_reason"].startswith("维护窗口：ecs-etl-02 扩容")
    # 修改：条件不再匹配 → 告警释放、补发通知
    body2 = {**body, "matchers": {"tenant_ids": [tenant_ids["tenant-a"]], "resource_refs": [ref + 100000]}}
    up = client.put(f"/api/v1/silences/{sid}", json=body2, headers=admin).json()["data"]
    assert up["released"] == 1
    with SessionLocal() as db:
        assert db.get(AlarmEvent, aid).status == "FIRING"
    assert client.put(f"/api/v1/silences/{sid}", json=body, headers=admin).json()["data"]["suppressed"] == 1
    # 取消：仍未恢复的告警转回告警中并补发通知
    c = client.post(f"/api/v1/silences/{sid}/cancel", headers=admin)
    assert c.status_code == 200 and c.json()["data"]["released"] == 1
    with SessionLocal() as db:
        assert db.get(AlarmEvent, aid).status == "FIRING"
        logs = [lg.action for lg in db.scalars(select(AlarmEventLog).where(AlarmEventLog.alarm_id == aid)
                                               .order_by(AlarmEventLog.id))]
        assert logs[-4:] == ["SILENCE", "UNSILENCE", "SILENCE", "UNSILENCE"]
        pending = db.scalars(select(Notification).where(Notification.alarm_id == aid,
                                                        Notification.status == "PENDING")).all()
        assert len(pending) == 2 and all(n.kind == "FIRING" for n in pending)
    assert client.post(f"/api/v1/silences/{sid}/cancel", headers=admin).status_code == 409
    assert client.put(f"/api/v1/silences/{sid}", json=body, headers=admin).status_code == 409
    actions = [x["action"] for x in client.get("/api/v1/system/audit-logs", params={"page_size": 10},
                                                headers=admin).json()["data"]["items"]]
    assert {"silence.create", "silence.update", "silence.cancel"} <= set(actions)


def test_quick_silence(client, tenant_ids):
    oncall = demo_login(client, "oncall")                 # 值班有 silence:manage
    with SessionLocal() as db:
        a = _alarm(db, "ecs-app-04", "R-ECS-002")
        aid = a.id
    r = client.post(f"/api/v1/alarms/{aid}/silence", json={"minutes": 60, "target": "resource"}, headers=oncall)
    assert r.status_code == 200 and r.json()["data"]["suppressed"] >= 1
    s = r.json()["data"]["silence"]
    assert s["name"].startswith("快速静默：ecs-app-04") and s["kind"] == "SILENCE" and s["created_by"] == "张伟"
    assert client.get(f"/api/v1/alarms/{aid}", headers=oncall).json()["data"]["status"] == "SUPPRESSED"
    bad = client.post(f"/api/v1/alarms/{aid}/silence", json={"minutes": 5}, headers=oncall)
    assert bad.status_code == 400                          # 至少 10 分钟
    viewer = demo_login(client, "viewer")
    assert client.post(f"/api/v1/alarms/{aid}/silence", json={"minutes": 60}, headers=viewer).status_code == 403
    wang = demo_login(client, "wangfang")                  # 租户B 用户看不到租户A 的告警
    assert client.post(f"/api/v1/alarms/{aid}/silence", json={"minutes": 60}, headers=wang).status_code == 404
    assert client.post(f"/api/v1/silences/{s['id']}/cancel", headers=oncall).status_code == 200


def _silence(**kw) -> Silence:
    now = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)
    base = {"name": "t", "kind": "MAINTENANCE", "matchers": {"services": ["ecs"]}, "recurrence": {},
            "starts_at": now - timedelta(days=1), "ends_at": now + timedelta(days=30), "created_by": "t"}
    return Silence(**{**base, **kw})


def test_weekly_window_across_midnight():
    # 每周四 22:00 至次日 02:00（平台时区 Asia/Shanghai）；2026-10-22 是周四
    s = _silence(recurrence={"weekdays": [4], "start": "22:00", "end": "02:00"})
    at = lambda d, h, m=0: datetime(2026, 10, d, h, m, tzinfo=UTC) - timedelta(hours=8)  # noqa: E731  本地 → UTC
    assert not silence_service.in_window(s, at(22, 21, 59))
    assert silence_service.in_window(s, at(22, 22, 0)) and silence_service.in_window(s, at(23, 1, 59))
    assert not silence_service.in_window(s, at(23, 2, 0)) and not silence_service.in_window(s, at(23, 22, 30))
    assert silence_service.status_of(s, at(22, 12)) == "WAITING" and silence_service.status_of(s, at(22, 23)) == "ACTIVE"
    start, end = silence_service.next_window(s, at(22, 12))
    assert (start, end) == (at(22, 22), at(23, 2))
    start, _ = silence_service.next_window(s, at(23, 3))
    assert start == at(29, 22)                             # 下周四
    day = _silence(recurrence={"weekdays": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"})
    assert silence_service.in_window(day, at(22, 10)) and not silence_service.in_window(day, at(24, 10))  # 周六
    assert silence_service.status_of(_silence(cancelled_at=at(22, 1)), at(22, 2)) == "CANCELLED"
    assert silence_service.status_of(_silence(starts_at=at(25, 0)), at(22, 2)) == "PENDING"


def test_expired_silence_releases_on_sweep(client, admin, tenant_ids):
    """静默到期后，通知进程每分钟的 sweep 把仍未恢复的告警转回 FIRING 并补发通知。"""
    with SessionLocal() as db:
        a = _alarm(db, "ecs-api-gw-01", "R-ECS-007")
        aid = a.id
    body = {"name": "短静默", "matchers": {"tenant_ids": [tenant_ids["tenant-a"]], "fingerprints": [a.fingerprint]},
            "ends_at": _iso(utcnow() + timedelta(minutes=30))}
    sid = client.post("/api/v1/silences", json=body, headers=admin).json()["data"]["id"]
    with SessionLocal() as db:
        assert db.get(AlarmEvent, aid).status == "SUPPRESSED"
        res = silence_service.sweep(db, utcnow() + timedelta(minutes=31))
        assert res["released"] >= 1
        assert db.get(AlarmEvent, aid).status == "FIRING"
        n = db.scalar(select(Notification).where(Notification.alarm_id == aid).order_by(Notification.id.desc()))
        assert n.status == "PENDING" and n.kind == "FIRING"
        s = db.get(Silence, sid)
        assert silence_service.status_of(s, utcnow() + timedelta(minutes=31)) == "EXPIRED"
        s.ends_at = utcnow() - timedelta(seconds=1)       # 让它真正结束，避免影响后续测试
        db.commit()
