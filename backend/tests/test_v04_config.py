"""v0.4 配置类功能：系统设置、采集开关（台账跳过 / 巡检跳过 / 告警自动关闭）、任务周期与 crontab、调度同步、
台账字典与 Excel 导入、接口探测工具。"""
import io
import json

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.hcs import factory
from app.ledger.sync import sync_ledger
from app.models import AlarmEvent, Resource, ResourceChange
from app.services import collect_switch
from app.services.pipeline import run_all
from app.services.tenancy import resolve_targets
from app.tools import probe
from app.workers.scheduler import JobSync, desired_jobs
from mock_hcs.app import app as mock_app

from .conftest import MOCK_URL, demo_login, login


# ---------------------------------------------------------------- 系统设置
def test_settings_view_and_permissions(client, admin):
    data = client.get("/api/v1/system/settings", headers=admin).json()["data"]
    assert set(data) == {"retention", "report", "platform_alarm", "ledger_dict"}
    assert data["retention"]["value"]["audit_log_days"] == 180 and data["retention"]["customized"] is False
    assert data["report"]["value"]["mail_to"] == ["ops-report@example.com", "zhaolei@example.com"]
    assert data["report"]["updated_by"] == "赵磊" and data["ledger_dict"]["value"]["business"]
    fields = {f["key"]: f for f in data["retention"]["fields"]}
    assert fields["audit_log_days"]["ge"] == 90 and fields["pod_event_days"]["le"] == 365
    oncall = demo_login(client, "oncall")
    assert set(client.get("/api/v1/system/settings", headers=oncall).json()["data"]) == {"report", "ledger_dict"}
    assert client.put("/api/v1/system/settings/retention", json={"audit_log_days": 200}, headers=oncall).status_code == 403
    lead = demo_login(client, "zhaolei")                 # 运维负责人：可改报表设置，不能改数据保留（平台管理员）
    assert client.put("/api/v1/system/settings/retention", json={"audit_log_days": 200}, headers=lead).status_code == 403


def test_settings_validation_and_audit(client, admin):
    bad = client.put("/api/v1/system/settings/retention", json={"audit_log_days": 30}, headers=admin)
    assert bad.status_code == 400 and "不能小于 90" in bad.json()["message"]
    bad = client.put("/api/v1/system/settings/platform_alarm", json={"mail_to": ["not-an-email"]}, headers=admin)
    assert bad.status_code == 400 and "邮箱格式不正确" in bad.json()["message"]
    bad = client.put("/api/v1/system/settings/report", json={"cron": {"DAILY": "61 8 * * *"}}, headers=admin)
    assert bad.status_code == 400 and "crontab" in bad.json()["message"]
    assert client.put("/api/v1/system/settings/nope", json={}, headers=admin).status_code == 404
    r = client.put("/api/v1/system/settings/retention", json={"pod_event_days": 45}, headers=admin)
    assert r.status_code == 200 and r.json()["data"]["changed"] == ["pod_event_days"]
    assert r.json()["data"]["value"]["pod_event_days"] == 45 and r.json()["data"]["updated_by"] == "系统管理员"
    logs = client.get("/api/v1/system/audit-logs", params={"action": "setting.update"}, headers=admin).json()["data"]
    assert logs["items"][0]["detail"]["changes"] == {"pod_event_days": [30, 45]}
    client.put("/api/v1/system/settings/retention", json={"pod_event_days": 30}, headers=admin)
    lina = demo_login(client, "lina")                    # 只有租户A 权限：全局设置不能改
    assert client.put("/api/v1/system/settings/report", json={"mail_to": []}, headers=lina).status_code == 403


# ---------------------------------------------------------------- 采集开关
def test_collect_switch_api(client, admin, tenant_ids):
    b = tenant_ids["tenant-b"]
    data = client.get("/api/v1/system/collect-switches", headers=admin).json()["data"]
    types = {t["code"]: t for t in data["types"]}
    assert len(types) == 18 and data["global_editable"] is True
    dgc = types["dgc_realtime_job"]["tenants"][str(b)]
    assert (dgc["setting"], dgc["effective"]) == (False, False) and types["dgc_realtime_job"]["global"] is True
    assert types["dcs_instance"]["tenants"][str(b)] == {"setting": None, "effective": True, "resources": 2,
                                                         "active_alarms": 0}
    meta = client.get("/api/v1/resources/types", headers=admin).json()["data"]
    gauss = next(t for t in meta["types"] if t["code"] == "gaussdb_instance")
    assert gauss["collect"]["off_tenants"] == ["租户B"] and gauss["collect"]["global"] is True

    body = {"items": [{"resource_type": "dcs_instance", "tenant_id": tenant_ids["tenant-a"], "enabled": False}]}
    imp = client.post("/api/v1/system/collect-switches/impact", json=body, headers=admin).json()["data"]
    assert imp["stop"] == ["租户A · DCS 缓存实例"] and imp["active_alarms"] == 2      # redis-prod-01 / redis-cache-02
    bad = client.post("/api/v1/system/collect-switches/impact", json={"items": [{"resource_type": "nope"}]},
                      headers=admin)
    assert bad.status_code == 400
    lina = demo_login(client, "lina")
    assert client.put("/api/v1/system/collect-switches", json=body, headers=lina).status_code == 403   # 值班没有权限
    # 自定义角色：有采集开关权限、只有租户A 的数据范围 → 只能改租户A 的设置
    role = client.post("/api/v1/system/roles", headers=admin, json={
        "code": "collect_a", "name": "租户A 采集管理", "permissions": ["task:view", "collect:manage"]}).json()["data"]
    assert client.post("/api/v1/system/users", headers=admin, json={
        "username": "collect_a01", "display_name": "采集管理员A", "password": "Init-pass-01", "role_id": role["id"],
        "tenant_ids": [tenant_ids["tenant-a"]]}).status_code == 200
    h = login(client, "collect_a01", "Init-pass-01")
    seen = client.get("/api/v1/system/collect-switches", headers=h).json()["data"]
    assert [t["code"] for t in seen["tenants"]] == ["tenant-a"] and seen["global_editable"] is False
    glob = {"items": [{"resource_type": "css_cluster", "tenant_id": 0, "enabled": False}]}
    assert client.put("/api/v1/system/collect-switches", json=glob, headers=h).status_code == 403   # 不能改全局
    other = {"items": [{"resource_type": "css_cluster", "tenant_id": b, "enabled": False}]}
    assert client.put("/api/v1/system/collect-switches", json=other, headers=h).status_code == 403  # 不能改其他租户
    mine = {"items": [{"resource_type": "css_cluster", "tenant_id": tenant_ids["tenant-a"], "enabled": False}]}
    r = client.put("/api/v1/system/collect-switches", json=mine, headers=h).json()["data"]
    assert r["changed"][0]["new"] is False and r["closed_alarms"] == 0
    mine["items"][0]["enabled"] = None                                   # 改回“跟随全局”
    assert client.put("/api/v1/system/collect-switches", json=mine, headers=h).json()["data"]["changed"]
    logs = client.get("/api/v1/system/audit-logs", params={"action": "collect.update"}, headers=admin).json()["data"]
    assert logs["items"][0]["operator"] == "采集管理员A" and "租户A · CSS" in logs["items"][0]["detail"]["changes"][0]


def test_collect_switch_skips_ledger_and_tasks(world, mem_db, mock_http):
    a, b = world["tenant-a"], world["tenant-b"]
    targets = {t.code: t for t in resolve_targets(mem_db)}
    for tg in targets.values():
        sync_ledger(mem_db, client=tg.client(http=mock_http, mock_url=MOCK_URL), target=tg)
    dcs_b = mem_db.scalar(select(func.count()).select_from(Resource).where(
        Resource.tenant_id == b.id, Resource.resource_type == "dcs_instance", Resource.lifecycle == "ACTIVE"))
    assert dcs_b == 2
    # 租户B 先产生一条 DCS 告警，再关闭租户B 的 DCS 采集 → 告警自动关闭
    run_all(mem_db, "dcs_status", http=mock_http, mock_url=MOCK_URL)
    alarm = AlarmEvent(fingerprint="t-dcs-b", rule_id="R-DCS-001", tenant_id=b.id, service="dcs",
                       resource_type="dcs_instance", resource_name="redis-b", sub_object="", severity="P1",
                       status="FIRING", title="实例状态异常", check_item="instance_status")
    mem_db.add(alarm)
    mem_db.commit()
    res = collect_switch.update(mem_db, [{"resource_type": "dcs_instance", "tenant_id": b.id, "enabled": False}], "测试")
    mem_db.commit()
    assert res["closed_alarms"] == 1 and mem_db.get(AlarmEvent, alarm.id).status == "CLOSED"
    runs = run_all(mem_db, "dcs_status", http=mock_http, mock_url=MOCK_URL)
    assert [r.tenant_id for r in runs] == [a.id]                       # 租户B 跳过，不产生运行记录
    tg = targets["tenant-b"]
    run = sync_ledger(mem_db, client=tg.client(http=mock_http, mock_url=MOCK_URL), target=tg)
    assert run.status == "SUCCESS" and "dcs_instance" in run.stats["skipped"]
    still = mem_db.scalar(select(func.count()).select_from(Resource).where(
        Resource.tenant_id == b.id, Resource.resource_type == "dcs_instance", Resource.lifecycle == "ACTIVE"))
    assert still == 2                                                   # 已有台账保留，不会被当成“已释放”
    # 全局关闭、租户A 单独开启：租户A 照常，租户B 跟随全局关闭
    collect_switch.update(mem_db, [{"resource_type": "dcs_instance", "tenant_id": 0, "enabled": False},
                                   {"resource_type": "dcs_instance", "tenant_id": a.id, "enabled": True},
                                   {"resource_type": "dcs_instance", "tenant_id": b.id, "enabled": None}], "测试")
    sw = collect_switch.load(mem_db)
    assert sw.enabled(a.id, "dcs_instance") and not sw.enabled(b.id, "dcs_instance")
    assert not sw.task_enabled(b.id, "dcs_status") and sw.task_enabled(b.id, "ecs_status")


# ---------------------------------------------------------------- 任务周期 / crontab / 调度同步
def test_task_edit_and_scheduler_sync(client, admin):
    tasks = {t["code"]: t for t in client.get("/api/v1/system/tasks", headers=admin).json()["data"]}
    assert tasks["data_cleanup"]["cron"] == "30 3 * * *" and tasks["ledger_sync"]["min_interval"] == 600
    assert set(tasks["dgc_realtime_status"]["skipped_tenants"]) == {"租户B"}
    cleanup = tasks["data_cleanup"]["runs"][0]
    assert cleanup["status"] == "SUCCESS" and cleanup["tenant_id"] is None
    oncall = demo_login(client, "oncall")
    assert client.put("/api/v1/system/tasks/dcs_status", json={"interval_sec": 300}, headers=oncall).status_code == 403
    assert client.put("/api/v1/system/tasks/ledger_sync", json={"interval_sec": 300}, headers=admin).status_code == 400
    assert client.put("/api/v1/system/tasks/dcs_status", json={"cron": "every 5 min"}, headers=admin).status_code == 400
    assert client.put("/api/v1/system/tasks/data_cleanup", json={"cron": ""}, headers=admin).status_code == 400
    assert client.put("/api/v1/system/tasks/nope", json={"enabled": False}, headers=admin).status_code == 404

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    sync = JobSync(sched, "Asia/Shanghai")
    first = sync.sync()
    assert {"dcs_status", "ledger_sync", "data_cleanup", "report_daily"} <= set(first["added"])
    with SessionLocal() as db:
        assert desired_jobs(db)["data_cleanup"]["sig"] == ("cron", "30 3 * * *")
    r = client.put("/api/v1/system/tasks/dcs_status", json={"interval_sec": 300, "cron": "*/10 * * * *"}, headers=admin)
    assert r.status_code == 200 and r.json()["data"]["changed"] is True
    assert sync.sync()["updated"] == ["dcs_status"]
    assert client.put("/api/v1/system/tasks/dcs_status", json={"enabled": False}, headers=admin).status_code == 200
    assert sync.sync()["removed"] == ["dcs_status"]
    client.put("/api/v1/system/tasks/dcs_status", json={"interval_sec": 60, "cron": "", "enabled": True}, headers=admin)
    assert sync.sync()["added"] == ["dcs_status"] and sync.sync() == {"added": [], "removed": [], "updated": []}
    logs = client.get("/api/v1/system/audit-logs", params={"action": "task.update"}, headers=admin).json()["data"]
    assert logs["total"] >= 3


def test_run_task_all_tenants_skipped(client, admin, tenant_ids):
    h = {**admin, "X-Tenant-Id": str(tenant_ids["tenant-b"])}
    r = client.post("/api/v1/system/tasks/gaussdb_status/run", headers=h)   # 租户B 已关闭 GaussDB 采集
    assert r.status_code == 409 and "关闭" in r.json()["message"]


# ---------------------------------------------------------------- 台账字典与 Excel 导入
def _template(client, h, **params) -> bytes:
    r = client.get("/api/v1/resources/import/template", params=params, headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
    return r.content


def _upload(client, h, content: bytes, **params):
    return client.post("/api/v1/resources/import", params=params, headers=h,
                       files={"file": ("ledger.xlsx", content, "application/vnd.openxmlformats-officedocument."
                                                               "spreadsheetml.sheet")})


def test_ledger_import_dry_run_and_apply(client, admin):
    wb = load_workbook(io.BytesIO(_template(client, admin, resource_type="dcs_instance")))
    ws = wb["台账管理字段"]
    header = [c.value for c in ws[1]]
    col = {h: i + 1 for i, h in enumerate(header)}
    rows = {ws.cell(r, col["名称"]).value: r for r in range(2, ws.max_row + 1)}
    r1, r2 = rows["redis-prod-01"], rows["redis-cache-02"]
    ws.cell(r1, col["负责人"]).value = "孙浩"
    ws.cell(r1, col["备注"]).value = "导入测试"
    ws.cell(r2, col["环境"]).value = "火星"                              # 错误行
    ws.cell(ws.max_row + 1, col["资源 ID"]).value = "not-exists"         # 不存在的资源
    buf = io.BytesIO()
    wb.save(buf)
    content = buf.getvalue()
    dry = _upload(client, admin, content).json()["data"]
    assert dry["dry_run"] is True and dry["changed"] == 1 and dry["error_count"] == 2
    assert dry["items"][0]["name"] == "redis-prod-01"
    assert {c["label"] for c in dry["items"][0]["changes"]} == {"负责人", "备注"}
    assert any("环境只能填" in e["message"] for e in dry["errors"])
    strict = _upload(client, admin, content, dry_run="false")
    assert strict.status_code == 400 and strict.json()["data"]["error_count"] == 2      # 有错误行默认不导入
    done = _upload(client, admin, content, dry_run="false", skip_errors="true").json()["data"]
    assert done["applied"] == 1
    page = client.get("/api/v1/resources", params={"keyword": "redis-prod-01"}, headers=admin).json()["data"]
    assert page["items"][0]["owner"] == "孙浩" and page["items"][0]["remark"] == "导入测试"
    with SessionLocal() as db:
        ch = db.scalar(select(ResourceChange).where(ResourceChange.name == "redis-prod-01",
                                                    ResourceChange.source == "IMPORT")
                       .order_by(ResourceChange.id.desc()))
        assert ch.operator == "系统管理员" and {c["field"] for c in ch.changes} == {"owner", "remark"}
    viewer = demo_login(client, "viewer")
    assert _upload(client, viewer, content).status_code == 403


def test_ledger_dict_enforce(client, admin):
    d = client.get("/api/v1/resources/dict", headers=admin).json()["data"]
    assert "孙浩" in [o["name"] for o in d["owners"]] and d["enforce"] is False
    r = client.put("/api/v1/resources/dict", json={**d, "enforce": True}, headers=admin)
    assert r.status_code == 200 and r.json()["data"]["enforce"] is True
    try:
        wb = load_workbook(io.BytesIO(_template(client, admin, resource_type="dcs_instance")))
        ws = wb["台账管理字段"]
        col = {c.value: i + 1 for i, c in enumerate(ws[1])}
        ws.cell(2, col["负责人"]).value = "不在字典里的人"
        buf = io.BytesIO()
        wb.save(buf)
        dry = _upload(client, admin, buf.getvalue()).json()["data"]
        assert dry["changed"] == 0 and "不在台账字典中" in dry["errors"][0]["message"]
        rid = client.get("/api/v1/resources", params={"keyword": "redis-prod-01"}, headers=admin).json()["data"]
        rid = rid["items"][0]["id"]
        bad = client.patch(f"/api/v1/resources/{rid}", json={"business": "不存在的系统"}, headers=admin)
        assert bad.status_code == 400 and "字典" in bad.json()["message"]
    finally:
        client.put("/api/v1/resources/dict", json={**d, "enforce": False}, headers=admin)


# ---------------------------------------------------------------- 接口探测工具
def test_probe_tool(tmp_path, monkeypatch, capsys):
    http = TestClient(mock_app, base_url=MOCK_URL)
    monkeypatch.setattr(probe, "client_for", lambda t, p, mock_url=None: factory.client_for(t, p, http=http,
                                                                                         mock_url=MOCK_URL))
    out = tmp_path / "probe-a.json"
    assert probe.main(["--tenant", "tenant-a", "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    res = {r["code"]: r for r in report["results"]}
    assert res["iam.auth"]["result"] == "OK" and res["ecs.servers"]["count"] == 5 and res["ecs.servers"]["total"] == 34
    assert res["ecs.servers"]["missing_required"] == [] and "ACTIVE" in res["ecs.servers"]["values"]["status"]
    sample = json.dumps(res["ecs.servers"]["sample"], ensure_ascii=False)
    assert "10.*.*." in sample and "secret" not in json.dumps(report).lower()
    assert res["ces.ecs_usage"]["result"] == "OK" and res["k8s.events"]["result"] == "OK"
    # 租户B：已关闭采集的类型跳过；没有 CES 记为 WARN（不算失败）
    assert probe.main(["--tenant", "tenant-b", "--out", str(out)]) == 0
    res = {r["code"]: r for r in json.loads(out.read_text(encoding="utf-8"))["results"]}
    assert res["gaussdb.instances"]["result"] == "SKIP" and res["ces.metrics"]["result"] == "WARN"
    assert probe.main(["--tenant", "nope"]) == 2
    capsys.readouterr()
    assert probe.main(["--list"]) == 0 and "tenant-b" in capsys.readouterr().out


def test_probe_field_check():
    item = {"id": "1", "flavor": {"vcpus": 2}, "attachments": [], "nodes": [{"status": "ok"}], "x": None}
    assert probe._walk(item, "flavor.vcpus") == ("ok", 2)
    assert probe._walk(item, "flavor.ram")[0] == "missing" and probe._walk(item, "x")[0] == "null"
    assert probe._walk(item, "attachments[].server_id")[0] == "empty"
    assert probe._values([item, {"nodes": [{"status": "bad"}, {"status": "ok"}]}], "nodes[].status") == {"ok": 2, "bad": 1}
    masked = probe._mask("", {"id": "0a1b2c3d4e5f40718293a4b5c6d7e8f9", "name": "ecs-core-db", "ip": "10.1.2.3",
                              "flavor_ref": "c6.large.2", "status": "ACTIVE"})
    assert masked == {"id": "0a1b…f9", "name": "ec***", "ip": "10.*.*.3", "flavor_ref": "c6.large.2", "status": "ACTIVE"}
