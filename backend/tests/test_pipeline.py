from email.message import EmailMessage

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.hcs import build_client
from app.models import AlarmEvent, Contact, ContactGroup, InspectionRun, Notification, NotifyRoute, Resource, User
from app.notify.dispatcher import dispatch_pending
from app.rules.loader import sync_default_rules
from app.services.pipeline import run_task
from mock_hcs import inventory
from mock_hcs.app import app as mock_app

SETTINGS = Settings(hcs_scheme="http", hcs_iam_url="http://mock", hcs_password="pwd", hcs_project_id="p1",
                    hcs_workspaces=["ws-bigdata"],
                    hcs_endpoints={k: "mock" for k in ("dataarts", "mrs", "gaussdb", "dcs", "ces")})


def _client():
    client = build_client(SETTINGS, http=TestClient(mock_app, base_url="http://mock"))
    client.mock_url = "http://mock"  # 明确该合成传输的 MRS elapsed_time 契约为毫秒。
    return client


def _setup(db):
    sync_default_rules(db)
    person = User(username="pipeline-recipient", display_name="张伟", email="zhangwei@example.com",
                  password_hash="!", login_enabled=False, notify_enabled=True)
    db.add(person)
    db.flush()
    db.add(Contact(id=1, user_id=person.id, name="张伟", email="zhangwei@example.com"))
    db.add(ContactGroup(code="ops", name="值班组", member_ids=[1], leader_ids=[]))
    db.add(NotifyRoute(name="兜底路由", priority=999, match={}, group_code="ops"))
    db.commit()


def test_dcs_pipeline_fire_and_notify(mem_db, monkeypatch):
    _setup(mem_db)
    monkeypatch.setitem(inventory.find("dcs", "redis-mq-01"), "status", "ERROR")   # 演示清单中默认正常，这里模拟故障
    client = _client()
    for _ in range(3):                      # R-DCS-005 需要连续 3 次命中
        run = run_task(mem_db, "dcs_status", client=client)
        assert run.status == "SUCCESS" and run.api_calls > 0
    alarms = {(a.resource_name, a.rule_id): a for a in mem_db.scalars(select(AlarmEvent))}
    assert alarms[("redis-mq-01", "R-DCS-001")].severity == "P1"                # ERROR 实例，首次即触发
    mem = alarms[("redis-prod-01", "R-DCS-005")]
    assert (mem.severity, mem.current_value, mem.title) == ("P2", "92.3%", "内存使用率 92.3%（≥90%）")
    assert ("redis-cache-02", "R-DCS-005") in alarms                              # 83.1% → P3
    res = mem_db.scalar(select(Resource).where(Resource.name == "redis-prod-01"))
    assert res.health == "MAJOR" and res.metrics["memory_usage"] == 92.3
    assert mem_db.scalar(select(InspectionRun).order_by(InspectionRun.id.desc())).abnormal_total == 3

    sent: list[EmailMessage] = []
    assert dispatch_pending(mem_db, sender=sent.append) == 3
    subjects = {m["Subject"] for m in sent}
    assert "【P2-重要】【DCS】redis-prod-01 内存使用率 92.3%（≥90%）" in subjects
    p1 = next(m for m in sent if "redis-mq-01" in m["Subject"])
    assert p1["X-Priority"] == "1" and p1["Message-ID"].startswith("<alarm-")
    assert all(n.status == "SENT" for n in mem_db.scalars(select(Notification)))


def test_dataarts_and_mrs_pipeline(mem_db):
    _setup(mem_db)
    client = _client()
    for _ in range(2):
        run_task(mem_db, "dgc_realtime_status", client=client)
    run = run_task(mem_db, "dgc_mrs_node_status", client=client)
    assert run.status == "SUCCESS"
    got = {(a.resource_name, a.rule_id, a.sub_object): a.severity for a in mem_db.scalars(select(AlarmEvent))}
    assert got[("rt_payment_risk", "R-DGC-RT-001", "")] == "P1"
    assert got[("rt_inventory_cdc", "R-DGC-RT-002", "")] == "P1"
    assert got[("rt_user_behavior", "R-DGC-RT-005", "rt_user_behavior_agg")] == "P2"
    assert got[("dws_settle_daily / mr_export", "R-MRS-JOB-001", "")] == "P2"


def test_hcs_failure_marks_run_failed(mem_db):
    _setup(mem_db)
    bad = Settings(**{**SETTINGS.model_dump(), "hcs_password": ""})
    run = run_task(mem_db, "dcs_status", client=build_client(bad, http=TestClient(mock_app, base_url="http://mock")))
    assert run.status == "FAILED" and "IAM" in run.error_summary
