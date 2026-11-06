"""作业报表契约、状态事实、租户隔离及查询预算。仅使用独立测试数据库。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, select
from sqlalchemy.dialects import postgresql

from app.api.v1.job_reports import router
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user, scope_of
from app.main import app as production_app
from app.models import AlarmEvent, HcsAccount, HcsProject, Resource
from app.models.base import utcnow
from app.services.job_report_service import JobReportFilters, _filtered_snapshot


@pytest.fixture()
def report_api(mem_db):
    for tid in (1, 2):
        mem_db.add(HcsAccount(id=tid, code=f"t{tid}", name=f"租户{tid}",
                              iam_endpoint="https://example.invalid", domain_name=f"t{tid}"))
    mem_db.flush()
    for pid, tid in ((11, 1), (12, 1), (21, 2)):
        mem_db.add(HcsProject(id=pid, account_id=tid, region_id="region-test", project_id=f"project-{pid}",
                              project_name=f"项目{pid}", external_domain="example.invalid"))
    mem_db.commit()
    user = CurrentUser(id=1, username="reader", display_name="reader", permissions=["report:view"], tenant_ids=[1])
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        yield client, user, app


def resource(db, kind="dgc_realtime_job", status="NORMAL", **extra):
    if "metrics" in extra:
        # 本夹具模拟已采集的本地当前快照，不以裸旧 metrics 冒充新鲜观测。
        extra["attrs"] = {**extra.get("attrs", {}), "metrics_monitoring": {
            "observed_at": extra.get("last_seen", utcnow()).isoformat(),
            "time_basis": "LOCAL_COLLECTION", "stale_after_sec": 900}}
    row = Resource(**{
        "service": "mrs" if kind == "mrs_job" else "dataarts", "resource_type": kind,
        "resource_id": uuid4().hex, "name": "作业", "tenant_id": 1, "project_ref": 11,
        "status_raw": status, "health": "OK", **extra,
    })
    db.add(row)
    db.commit()
    return row


def alarm(db, row, status="FIRING", tenant_id=1):
    db.add(AlarmEvent(tenant_id=tenant_id, resource_ref=row.id, resource_type=row.resource_type,
                      resource_name=row.name, service=row.service, fingerprint=uuid4().hex, rule_id="test-job",
                      severity="P2", status=status, title="测试告警", check_item="job_status"))
    db.commit()


def report(client, panel="dgc", **params):
    response = client.get(f"/api/v1/job-reports/{panel}", params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0 and body["message"] == "ok"
    return body["data"]


@pytest.mark.parametrize(("kind", "raw", "display", "label"), [
    ("dgc_realtime_job", "NORMAL", "RUNNING", "运行中"),
    ("dgc_realtime_job", "RUNNING", "RUNNING", "运行中"),
    ("dgc_realtime_job", "STARTING", "STARTING", "启动中"),
    ("dgc_realtime_job", "STOPPING", "STOPPING", "停止中"),
    ("dgc_realtime_job", "STOPPED", "STOPPED", "已停止"),
    ("dgc_realtime_job", "FAILED", "FAILED", "失败"),
    ("dgc_realtime_job", "EXCEPTION", "EXCEPTION", "运行异常"),
    ("dgc_batch_job", "success", "SUCCEEDED", "成功"),
    ("dgc_batch_job", "fail", "FAILED", "失败"),
    ("dgc_batch_job", "running", "RUNNING", "运行中"),
    ("dgc_batch_job", "running-exception", "EXCEPTION", "运行异常"),
    ("dgc_batch_job", "WAITING", "WAITING", "等待中"),
    ("dgc_batch_job", "QUEUED", "QUEUED", "排队中"),
    ("dgc_batch_job", "PENDING", "PENDING", "待处理"),
    ("dgc_batch_job", "SUBMITTED", "SUBMITTED", "已提交"),
    ("dgc_batch_job", "CANCELED", "CANCELED", "已取消"),
    ("dgc_batch_job", "CANCELLED", "CANCELED", "已取消"),
    ("mrs_job", "ACCEPTED", "QUEUED", "排队中"),
    ("mrs_job", "SUBMITTED", "SUBMITTED", "已提交"),
    ("mrs_job", "NEW", "NEW", "新建"),
    ("mrs_job", "KILLED", "KILLED", "已终止"),
    ("mrs_job", "KILLING", "KILLING", "终止中"),
    ("mrs_job", "SUCCEEDED", "SUCCEEDED", "成功"),
    ("mrs_job", "SUCCESS", "SUCCEEDED", "成功"),
    ("mrs_job", "FINISHED", "FINISHED", "执行结束（结果未确认）"),
])
def test_known_text_states(mem_db, report_api, kind, raw, display, label):
    client, _, _ = report_api
    resource(mem_db, kind, raw)
    data = report(client, "mrs" if kind == "mrs_job" else "dgc")
    row = data["items"][0]
    assert (row["status_raw"], row["display_status"], row["status_label"], row["status_known"]) == (
        raw, display, label, True)
    assert data["summary"]["status_counts"] == {display: 1}
    assert data["summary"]["unknown_total"] == 0


@pytest.mark.parametrize(("state", "result", "display", "known"), [
    ("RUNNING", "UNDEFINED", "RUNNING", True),
    ("ACCEPTED", "UNDEFINED", "QUEUED", True),
    ("FINISHED", "SUCCEEDED", "SUCCEEDED", True),
    ("FINISHED", "FAILED", "FAILED", True),
    ("FINISHED", "UNDEFINED", "FINISHED", True),
    ("FINISHED", None, "FINISHED", True),
    ("FINISHED", "new-result", "new-result", False),
    ("FAILED", "SUCCEEDED", "FAILED", True),
    ("RUNNING", "SUCCEEDED", "RUNNING", True),
    (None, "UNDEFINED", "UNKNOWN", True),
    (None, None, "NO_DATA", False),
])
def test_mrs_execution_state_and_result(mem_db, report_api, state, result, display, known):
    client, _, _ = report_api
    resource(mem_db, "mrs_job", state, attrs={"job_state": state, "job_result": result})
    row = report(client, "mrs")["items"][0]
    assert row["execution_state"] == state and row["execution_result"] == result
    assert row["status_raw"] == state
    assert (row["display_status"], row["status_known"]) == (display, known)


def test_unknown_missing_legacy_and_empty_data_are_not_success(mem_db, report_api):
    client, _, _ = report_api
    empty = report(client, "mrs")
    assert empty["summary"] == {"total": 0, "status_counts": {}, "by_source": {}, "by_engine": {}, "by_type": {},
                                "unknown_total": 0, "no_data_total": 0, "active_alarms": 0,
                                "latest_update_at": None, "data_status": "NO_DATA"}
    for raw in ("VendorCustom", "42", "UNDEFINED", None, "", "   "):
        resource(mem_db, "mrs_job", raw)
    data = report(client, "mrs")
    assert data["summary"]["unknown_total"] == 3
    assert data["summary"]["no_data_total"] == 3
    assert data["summary"]["status_counts"] == {"VendorCustom": 1, "42": 1, "UNKNOWN": 1, "NO_DATA": 3}
    for row in data["items"]:
        assert row["health"] == "UNKNOWN" and row["display_status"] != "SUCCEEDED"
        assert row["execution_state"] is None and row["execution_result"] is None
        if row["status_raw"] in ("VendorCustom", "42"):
            assert row["display_status"] == row["status_label"] == row["status_raw"]
            assert row["status_known"] is False


def test_explicit_missing_new_state_does_not_show_stale_success(mem_db, report_api):
    client, _, _ = report_api
    resource(mem_db, "dgc_batch_job", "success", attrs={"instance_status": None}, metrics={"run_minutes": None})
    row = report(client, "batch")["items"][0]
    assert row["status_raw"] == "success"
    assert row["execution_state"] is None and row["display_status"] == "NO_DATA"
    assert row["metrics"] == {"run_minutes": None, "p90_minutes": None}


def test_panel_membership_requires_actual_flink_engine_or_type(mem_db, report_api):
    client, _, _ = report_api
    rt = resource(mem_db, name="flink in name is insufficient", attrs={"job_type": "REAL_TIME"})
    batch = resource(mem_db, "dgc_batch_job", "success")
    flink = resource(mem_db, "mrs_job", "RUNNING", attrs={"job_type": "Flink"})
    spark = resource(mem_db, "mrs_job", "SUCCEEDED", attrs={"job_type": "SparkSubmit"})
    rt_flink = resource(mem_db, attrs={"engine": " FLINK "})
    typed = resource(mem_db, "dgc_batch_job", "running", attrs={"type": "flink"})
    resource(mem_db, "mrs_cluster", "running", attrs={"engine": "flink"})
    resource(mem_db, "mrs_job", "RUNNING", lifecycle="DELETED", attrs={"job_type": "Flink"})
    expected = {
        "dgc": {rt.id, batch.id, rt_flink.id, typed.id}, "realtime": {rt.id, rt_flink.id},
        "batch": {batch.id, typed.id}, "mrs": {flink.id, spark.id}, "flink": {flink.id, rt_flink.id, typed.id},
    }
    for panel, ids in expected.items():
        data = report(client, panel)
        assert {row["id"] for row in data["items"]} == ids
        assert data["total"] == data["summary"]["total"] == len(ids)
        assert sum(data["summary"]["by_type"].values()) == len(ids)
    assert report(client, "dgc", page_size=1)["summary"]["by_type"] == {
        "dgc_realtime_job": 2, "dgc_batch_job": 2,
    }
    assert report(client, "dgc", resource_type="dgc_batch_job")["summary"]["by_type"] == {"dgc_batch_job": 2}
    assert report(client, "flink", lifecycle="ALL")["total"] == 4
    assert report(client, "mrs", engine="spark")["items"][0]["id"] == spark.id
    assert report(client, "realtime", job_type="real_time")["items"][0]["engine"] is None


def test_filtered_summary_dimensions_and_stable_pagination(mem_db, report_api):
    client, _, _ = report_api
    now = utcnow()
    rows = [resource(mem_db, "dgc_batch_job", raw, name=f"needle-{i}", last_seen=now,
                     attrs={"engine": "Flink", "job_type": "BATCH"})
            for i, raw in enumerate(("success", "fail", "success", "WAITING"))]
    resource(mem_db, "dgc_batch_job", "success", name="irrelevant", attrs={"engine": "Flink"})
    alarm(mem_db, rows[0])
    alarm(mem_db, rows[2], "ACKED")
    query = {"keyword": "needle", "status": ["SUCCEEDED", "FAILED"], "engine": "flink", "page_size": 1}
    first = report(client, "batch", **query)
    second = report(client, "batch", page=2, **query)
    assert [first["items"][0]["id"], second["items"][0]["id"]] == [rows[2].id, rows[1].id]
    assert first["summary"] == second["summary"]
    assert first["total"] == 3
    assert first["summary"]["status_counts"] == {"SUCCEEDED": 2, "FAILED": 1}
    assert first["summary"]["by_source"] == {"dataarts": 3}
    assert first["summary"]["by_engine"] == {"Flink": 3}
    assert first["summary"]["active_alarms"] == 2
    assert report(client, "batch", page=99, **query)["items"] == []
    assert report(client, "batch", status_raw="success", keyword="needle")["total"] == 2
    assert report(client, "batch", has_alarms=True)["total"] == 2
    assert report(client, "batch", has_alarms=False)["total"] == 3
    assert report(client, "batch", resource_type="mrs_job")["total"] == 0
    resource(mem_db, name="literal%_")
    assert report(client, keyword="%_")["total"] == 1


def test_tenant_project_and_alarm_isolation(mem_db, report_api):
    client, user, _ = report_api
    own = resource(mem_db, resource_id="same-name")
    other_project = resource(mem_db, resource_id="same-name", project_ref=12)
    other = resource(mem_db, resource_id="same-name", tenant_id=2, project_ref=21)
    global_row = resource(mem_db, tenant_id=None, project_ref=None)
    for state in ("FIRING", "ACKED", "SUPPRESSED", "RESOLVED", "CLOSED"):
        alarm(mem_db, own, state)
    alarm(mem_db, own, tenant_id=2)  # 历史错误租户关联不能泄露，也不能污染统计。
    alarm(mem_db, other, tenant_id=2)
    data = report(client)
    assert {row["id"] for row in data["items"]} == {own.id, other_project.id}
    assert data["summary"]["active_alarms"] == 3
    assert report(client, project_ref=12)["items"][0]["active_alarms"] == 0
    assert report(client, tenant_id=2)["total"] == 0
    assert report(client, project_ref=21)["total"] == 0
    assert client.get("/api/v1/job-reports/dgc", headers={"X-Tenant-Id": "2"}).status_code == 403
    user.tenant_ids = None
    full = report(client)
    assert {row["id"] for row in full["items"]} == {own.id, other.id, other_project.id, global_row.id}
    assert full["summary"]["active_alarms"] == 4
    selected = client.get("/api/v1/job-reports/dgc", headers={"X-Tenant-Id": "2"}).json()["data"]
    assert selected["total"] == 1 and selected["items"][0]["tenant_name"] == "租户2"
    user.tenant_ids = []
    assert report(client)["total"] == 0


def test_details_observed_metrics_and_stale_time_are_preserved(mem_db, report_api):
    client, _, _ = report_api
    observed = utcnow() - timedelta(days=9)
    attrs = {"workspace": "workspace-a", "instance_id": 7, "instance_status": "running",
             "plan_time": 1792800000000, "start_time": 1792800001000, "end_time": None,
             "nodes": [{"name": "node-a", "status": "EXCEPTION", "connection": "not-for-report"}],
             "arbitrary_private_field": "not-for-report"}
    row = resource(mem_db, "dgc_batch_job", "running", attrs=attrs, metrics={"run_minutes": None},
                   last_seen=observed, project_ref=21)
    before = dict(row.attrs)
    data = report(client, "batch")
    item = data["items"][0]
    assert item["updated_at"] == observed.isoformat().replace("+00:00", "Z")
    assert data["summary"]["latest_update_at"] == item["updated_at"]
    assert item["metrics"] == {"run_minutes": None, "p90_minutes": None}
    assert item["status_details"]["start_time"] == 1792800001000
    assert item["status_details"]["end_time"] is None
    assert item["status_details"]["nodes"] == [{"name": "node-a", "status": "EXCEPTION"}]
    assert "not-for-report" not in str(data)
    assert item["project_id"] is None and item["project_name"] is None
    assert item["tenant_name"] == "租户1"
    assert row.attrs == before and row.last_seen == observed  # 查询无写入。
    assert any("最后观测时间" in note for note in data["data_notes"])


def test_permissions_validation_and_route_registration(report_api):
    client, user, app = report_api
    assert report(client)["total"] == 0  # 单独 report:view 已足够。
    for params in ({"page": 0}, {"page_size": 101}, {"page_size": 0}, {"project_ref": -1},
                   {"lifecycle": "BOGUS"}, {"resource_type": "ecs_server"}, {"tenant_id": 0}):
        response = client.get("/api/v1/job-reports/dgc", params=params)
        assert response.status_code == 400 and response.json()["code"] == 40001
    assert client.get("/api/v1/job-reports/invalid").status_code == 400
    assert client.post("/api/v1/job-reports/dgc").status_code == 405
    user.permissions = ["ledger:view"]
    assert client.get("/api/v1/job-reports/dgc").status_code == 403
    app.dependency_overrides.pop(get_current_user)
    assert client.get("/api/v1/job-reports/dgc").status_code == 401
    operation = production_app.openapi()["paths"]["/api/v1/job-reports/{panel}"]["get"]
    assert "JobReportOut" in str(operation["responses"]["200"])


def test_large_report_uses_sql_pagination_and_two_selects(mem_db, report_api):
    client, user, _ = report_api
    mem_db.execute(insert(Resource), [{"tenant_id": 1, "project_ref": 11, "service": "dataarts",
                                      "resource_type": "dgc_batch_job", "resource_id": str(i), "name": str(i),
                                      "status_raw": "success", "last_seen": utcnow()} for i in range(1200)])
    mem_db.commit()
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(mem_db.bind, "before_cursor_execute", capture)
    try:
        data = report(client, "batch", page=3, page_size=100)
    finally:
        event.remove(mem_db.bind, "before_cursor_execute", capture)
    assert data["total"] == 1200 and len(data["items"]) == 100
    assert len(statements) == 2
    assert "LIMIT" in statements[1].upper() and "OFFSET" in statements[1].upper()
    # PG 与 SQLite 使用同一套聚合/分页；JSON 存在性表达式使用对应方言。
    query = _filtered_snapshot(scope_of(user, None), "flink", JobReportFilters(status=["RUNNING"]), "postgresql")
    compiled = str(select(query).compile(dialect=postgresql.dialect()))
    assert "JSONB" in compiled and "?" in compiled
