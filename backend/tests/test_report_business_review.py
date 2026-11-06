"""日月报周期、统计维度、导出范围和重新生成状态的独立回归。"""
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.api.v1 import reports
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_current_user
from app.main import app as production_app
from app.models import AlarmEvent, HcsAccount, InspectionRun
from app.reports import builder, job_excel, service


@pytest.fixture(autouse=True)
def utc_reports(monkeypatch):
    monkeypatch.setattr(builder, "_tz", lambda: ZoneInfo("UTC"))


def add_alarm(db, at, *, tenant_id=1, resource_ref=1):
    row = AlarmEvent(tenant_id=tenant_id, resource_ref=resource_ref, fingerprint=f"{tenant_id}-{at}-{resource_ref}",
                     rule_id="rule", service="ecs", resource_type="ecs_server", resource_name="same-name",
                     severity="P2", status="FIRING", title="CPU", check_item="cpu", first_seen=at, last_seen=at)
    db.add(row)
    return row


def add_run(db, at, tenant_id=1):
    db.add(InspectionRun(task_code="ecs", tenant_id=tenant_id, started_at=at,
                         finished_at=at + timedelta(seconds=1), status="SUCCESS"))


@pytest.mark.parametrize("ref,previous", [
    (date(2026, 3, 1), datetime(2026, 2, 1, tzinfo=UTC)),
    (date(2026, 2, 1), datetime(2026, 1, 1, tzinfo=UTC)),
    (date(2024, 3, 1), datetime(2024, 2, 1, tzinfo=UTC)),
])
def test_monthly_comparison_uses_entire_previous_calendar_month(mem_db, ref, previous):
    start, end = builder.period_of("MONTHLY", ref)
    add_run(mem_db, previous - timedelta(days=40))
    add_alarm(mem_db, previous + timedelta(hours=1))
    add_alarm(mem_db, start - timedelta(hours=1))
    add_alarm(mem_db, previous - timedelta(hours=1))
    mem_db.commit()
    data = builder.build_report(mem_db, "MONTHLY", start, end)
    assert data["previous"]["new_alarms"] == 2


def test_comparison_readiness_does_not_use_other_tenants_history(mem_db):
    start, end = builder.period_of("DAILY", date(2026, 10, 20))
    add_run(mem_db, start - timedelta(days=30), tenant_id=2)
    add_run(mem_db, start, tenant_id=1)
    mem_db.commit()
    data = builder.build_report(mem_db, "DAILY", start, end, tenant_id=1)
    assert data["previous"] is None and data["compare"]["new_alarms"] is None


def test_top_resources_distinguishes_tenants_and_resource_ids(mem_db):
    start, end = builder.period_of("DAILY", date(2026, 10, 20))
    for tenant, resource in [(1, 11), (1, 12), (2, 13)]:
        add_alarm(mem_db, start + timedelta(hours=1), tenant_id=tenant, resource_ref=resource)
    mem_db.commit()
    top = builder.build_report(mem_db, "DAILY", start, end)["top_resources"]
    assert len(top) == 3 and all(row["count"] == 1 for row in top)


def test_regeneration_clears_delivery_state_of_replaced_contents(mem_db):
    row = service.generate(mem_db, "DAILY", date(2026, 10, 20))
    row.sent_at = datetime(2026, 10, 21, tzinfo=UTC)
    row.sent_to, row.send_error = ["review@example.invalid"], "previous attempt failed"
    mem_db.commit()
    regenerated = service.generate(mem_db, "DAILY", date(2026, 10, 20))
    assert regenerated.id == row.id
    assert regenerated.sent_at is None and regenerated.sent_to == [] and regenerated.send_error is None


def test_job_export_summary_describes_selected_scope_even_for_full_access(mem_db, monkeypatch):
    user = CurrentUser(id=1, username="reviewer", display_name="reviewer")
    monkeypatch.setattr(job_excel, "list_job_report_rows", lambda *a: [])
    data = job_excel.job_workbook(mem_db, Scope([7], True, user), "dgc",
                                   job_excel.JobReportFilters())
    rows = dict(load_workbook(BytesIO(data))["汇总"].values)
    assert rows["租户范围"] == "7"


def test_report_today_validation_uses_platform_timezone(mem_db, monkeypatch):
    app = FastAPI()
    app.include_router(reports.router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=1, username="reviewer", display_name="reviewer", permissions=["report:manage"])
    monkeypatch.setattr(reports, "utcnow", lambda: datetime(2026, 10, 26, 16, 30, tzinfo=UTC))
    monkeypatch.setattr(reports, "get_settings", lambda: SimpleNamespace(timezone="Asia/Shanghai"))
    response = TestClient(app).post("/api/v1/reports", json={"kind": "DAILY", "date": "2026-10-27"})
    assert response.status_code == 200, response.text


def test_report_and_ledger_exports_enforce_tenant_scope(mem_db):
    mem_db.add_all([HcsAccount(id=i, code=f"review-{i}", name=f"tenant-{i}", domain_name="",
                              iam_endpoint="https://iam.example.invalid") for i in (1, 2)])
    mem_db.commit()
    own = service.generate(mem_db, "LEDGER", tenant_id=1)
    foreign = service.generate(mem_db, "LEDGER", tenant_id=2)
    merged = service.generate(mem_db, "LEDGER")
    app = FastAPI()
    app.include_router(reports.router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=1, username="reader", display_name="reader", tenant_ids=[1], permissions=["report:view"])
    client = TestClient(app)
    assert client.get(f"/api/v1/reports/{own.id}/export").status_code == 200
    for row in (foreign, merged):
        assert client.get(f"/api/v1/reports/{row.id}").status_code == 404
        assert client.get(f"/api/v1/reports/{row.id}/export").status_code == 404
