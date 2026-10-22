"""报表中心：预生成报表、生成（同周期覆盖）、内容口径、导出 Excel、邮件发送（正文 + 附件）、权限。"""
import io
from email.message import EmailMessage

from openpyxl import load_workbook

from app.reports import service as report_service

from .conftest import demo_login, login


def _reports(client, h, **params):
    return client.get("/api/v1/reports", params=params, headers=h).json()["data"]


def test_seeded_reports(client, admin, tenant_ids):
    page = _reports(client, admin, page_size=50)
    kinds = [r["kind"] for r in page["items"]]
    assert page["total"] == 22 and kinds.count("DAILY") == 13 and kinds.count("WEEKLY") == 5
    assert kinds.count("MONTHLY") == 3 and kinds.count("LEDGER") == 1
    merged = [r["kind"] for r in _reports(client, admin, tenant_id="global", page_size=50)["items"]]
    assert (merged.count("DAILY"), merged.count("WEEKLY"), merged.count("MONTHLY")) == (7, 3, 1)
    b = _reports(client, admin, tenant_id=tenant_ids["tenant-b"], page_size=50)["items"]
    assert len(b) == 5 and all(r["tenant_name"] == "租户B" and "租户B" in r["title"] for r in b)
    monthly = next(r for r in page["items"] if r["kind"] == "MONTHLY" and r["tenant_id"] is None)
    data = client.get(f"/api/v1/reports/{monthly['id']}", headers=admin).json()["data"]["data"]
    assert data["previous"] is None and data["compare"]["new_alarms"] is None
    weekly_id = _reports(client, admin, kind="WEEKLY", tenant_id="global")["items"][0]["id"]
    weekly = client.get(f"/api/v1/reports/{weekly_id}", headers=admin).json()["data"]["data"]
    assert weekly["previous"] is not None and len(weekly["trend"]["labels"]) == 7
    assert len(weekly["by_tenant"]) == 2 and weekly["ecs"] and weekly["pods"]


def test_generate_daily_and_overwrite(client, admin, tenant_ids):
    r1 = client.post("/api/v1/reports", json={"kind": "DAILY"}, headers=admin).json()["data"]
    d = r1["data"]
    assert r1["title"].startswith("HCS 巡检日报") and r1["tenant_id"] is None and len(d["trend"]["labels"]) == 24
    assert {"overview", "compare", "by_service", "top_resources", "top_rules", "key_alarms", "tasks", "capacity",
            "ledger", "ecs", "pods", "by_tenant", "conclusion"} <= set(d)
    assert d["overview"]["new_alarms"] == sum(sum(v) for v in d["trend"]["series"].values())
    assert d["conclusion"] and d["ledger"]["total"] == 196 and len(d["by_tenant"]) == 2
    r2 = client.post("/api/v1/reports", json={"kind": "DAILY"}, headers=admin).json()["data"]
    assert r2["id"] == r1["id"]
    tb = client.post("/api/v1/reports", json={"kind": "DAILY", "tenant_id": tenant_ids["tenant-b"]},
                     headers=admin).json()["data"]
    assert tb["id"] != r1["id"] and tb["tenant_id"] == tenant_ids["tenant-b"] and "租户B" in tb["title"]
    assert tb["data"]["ledger"]["total"] == 58 and tb["data"]["by_tenant"] == []
    assert client.post("/api/v1/reports", json={"kind": "DAILY", "date": "2099-01-01"}, headers=admin).status_code == 400
    assert client.post("/api/v1/reports", json={"kind": "YEARLY"}, headers=admin).status_code == 400


def test_export_report_excel(client, admin, tenant_ids):
    rid = _reports(client, admin, kind="WEEKLY", tenant_id="global")["items"][0]["id"]
    wb = load_workbook(io.BytesIO(client.get(f"/api/v1/reports/{rid}/export", headers=admin).content))
    assert wb.sheetnames == ["概览", "告警趋势", "按服务", "重点告警", "Top 资源与规则", "巡检任务", "容量水位", "ECS 状态",
                             "容器事件", "分租户", "台账变更"]
    rid = _reports(client, admin, kind="WEEKLY", tenant_id=tenant_ids["tenant-a"])["items"][0]["id"]
    wb = load_workbook(io.BytesIO(client.get(f"/api/v1/reports/{rid}/export", headers=admin).content))
    assert "分租户" not in wb.sheetnames and "ECS 状态" in wb.sheetnames
    ledger = _reports(client, admin, kind="LEDGER")["items"][0]["id"]
    wb = load_workbook(io.BytesIO(client.get(f"/api/v1/reports/{ledger}/export", headers=admin).content))
    assert wb.sheetnames[:3] == ["概览", "台账变更", "汇总"] and "云硬盘 EVS" in wb.sheetnames


def test_send_report_mail(client, admin, monkeypatch):
    sent: list[EmailMessage] = []
    monkeypatch.setattr(report_service, "smtp_send", sent.append)
    rid = _reports(client, admin, kind="DAILY", tenant_id="global")["items"][0]["id"]
    r = client.post(f"/api/v1/reports/{rid}/send", json={}, headers=admin)        # 合并报表：发给“报表设置”里的收件人
    assert r.status_code == 200 and r.json()["data"]["sent_to"] == ["ops-report@example.com", "zhaolei@example.com"]
    sent.clear()
    assert client.post(f"/api/v1/reports/{rid}/send", json={"to": ["bad"]}, headers=admin).status_code == 400
    r = client.post(f"/api/v1/reports/{rid}/send", json={"to": ["ops@example.com"]}, headers=admin)
    assert r.status_code == 200 and r.json()["data"]["sent_to"] == ["ops@example.com"]
    msg = sent[0]
    assert msg["Subject"].startswith("【PROD】HCS 巡检日报") and msg["To"] == "ops@example.com"
    files = [p.get_filename() for p in msg.iter_attachments()]
    assert files and files[0].endswith(".xlsx")
    html = msg.get_body(("html",)).get_content()
    assert "结论" in html and "资源台账" in html


def test_member_permissions(client, admin):
    member = login(client, "oncall", "oncall123")
    created = client.post("/api/v1/reports", json={"kind": "LEDGER"}, headers=member)
    assert created.status_code == 200 and created.json()["data"]["created_by"] == "张伟"   # 成员可以生成 / 导出
    rid = created.json()["data"]["id"]
    assert client.delete(f"/api/v1/reports/{rid}", headers=member).status_code == 403
    assert client.delete(f"/api/v1/reports/{rid}", headers=admin).status_code == 200


def test_schedule_info(client):
    member = login(client, "oncall", "oncall123")
    data = client.get("/api/v1/reports/schedule", headers=member).json()["data"]
    assert set(data["cron"]) == {"DAILY", "WEEKLY", "MONTHLY"} and data["kinds"]["LEDGER"] == "资源台账报表"


def test_report_tenant_scope(client, tenant_ids):
    """只有租户A 权限的值班人员：只看到租户A 的报表，不能生成其他租户 / 全部租户合并报表。"""
    lina = demo_login(client, "lina")
    items = _reports(client, lina, page_size=50)["items"]
    assert items and {r["tenant_id"] for r in items} == {tenant_ids["tenant-a"]}
    other = client.post("/api/v1/reports", json={"kind": "DAILY", "tenant_id": tenant_ids["tenant-b"]}, headers=lina)
    assert other.status_code == 403 and other.json()["code"] == 40301
    own = client.post("/api/v1/reports", json={"kind": "DAILY"}, headers=lina).json()["data"]
    assert own["tenant_id"] == tenant_ids["tenant-a"] and "租户A" in own["title"]
    merged = _reports(client, demo_login(client, "admin"), tenant_id="global", kind="DAILY")["items"][0]["id"]
    assert client.get(f"/api/v1/reports/{merged}", headers=lina).status_code == 404
    viewer = demo_login(client, "viewer")
    assert client.post("/api/v1/reports", json={"kind": "DAILY"}, headers=viewer).status_code == 403
