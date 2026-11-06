"""未认领升级：告警 FIRING 超过 after_min 分钟仍无人认领 → 通知组长（每级只升级一次）；级别不匹配 / 已认领不升级。"""
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.hcs import build_client
from app.models import AlarmEvent, AlarmEventLog, Contact, ContactGroup, Notification, NotifyRoute, User
from app.notify.escalation import escalate_unacked, escalation_plan
from app.rules.loader import sync_default_rules
from app.services.pipeline import run_task
from mock_hcs import inventory
from mock_hcs.app import app as mock_app

from .test_pipeline import SETTINGS


def _client(settings: Settings = SETTINGS):
    return build_client(settings, http=TestClient(mock_app, base_url="http://mock"))


def _setup(db):
    sync_default_rules(db)
    member = User(username="escalation-member", display_name="张伟", email="zhangwei@example.com",
                  password_hash="!", login_enabled=False, enabled=True, notify_enabled=True, tenant_ids=None)
    leader = User(username="escalation-leader", display_name="赵磊", email="zhaolei@example.com",
                  password_hash="!", login_enabled=False, enabled=True, notify_enabled=True, tenant_ids=None)
    db.add_all([member, leader])
    db.flush()
    db.add_all([Contact(id=1, user_id=member.id, name=member.display_name, email=member.email),
                Contact(id=2, user_id=leader.id, name=leader.display_name, email=leader.email)])
    db.add(ContactGroup(code="ops", name="值班组", member_ids=[1], leader_ids=[2]))
    db.add(NotifyRoute(name="默认路由", priority=100, match={}, group_code="ops",
                       escalation=[{"after_min": 15, "to": "leader", "severities": ["P1", "P2"]}]))
    db.commit()


def _logs(db, alarm):
    return [x.action for x in db.scalars(select(AlarmEventLog).where(AlarmEventLog.alarm_id == alarm.id))]


def test_escalate_unacked(mem_db, monkeypatch):
    _setup(mem_db)
    monkeypatch.setitem(inventory.find("dcs", "redis-mq-01"), "status", "ERROR")
    client = _client()
    for _ in range(3):
        run_task(mem_db, "dcs_status", client=client)
    alarms = {a.resource_name: a for a in mem_db.scalars(select(AlarmEvent))}
    mem, down = alarms["redis-prod-01"], alarms["redis-mq-01"]          # P2 内存使用率 / P1 实例异常
    plan = escalation_plan(mem_db, mem)
    assert [p["names"] for p in plan] == [["赵磊"]] and plan[0]["note"] == "15 分钟未认领，升级通知组长 赵磊"
    low = [a for a in alarms.values() if a.severity not in ("P1", "P2")]
    assert all(escalation_plan(mem_db, a) == [] for a in low)
    down.status, down.ack_by = "ACKED", "张伟"                          # 已认领的不升级
    mem_db.commit()
    t0 = max(a.first_seen for a in alarms.values())
    assert escalate_unacked(mem_db, now=t0 + timedelta(minutes=10)) == 0
    assert escalate_unacked(mem_db, now=t0 + timedelta(minutes=16)) >= 1
    assert _logs(mem_db, mem).count("ESCALATE") == 1 and "ESCALATE" not in _logs(mem_db, down)
    n = mem_db.scalar(select(Notification).where(Notification.alarm_id == mem.id, Notification.kind == "ESCALATE"))
    assert n.recipients == ["zhaolei@example.com"] and n.status == "PENDING" and "升级" in n.subject
    assert escalate_unacked(mem_db, now=t0 + timedelta(hours=2)) == 0     # 每级只升级一次
