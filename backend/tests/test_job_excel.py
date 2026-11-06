"""真实 xlsx 文件、全量筛选、单元格类型与租户隔离。"""

from datetime import datetime
from io import BytesIO
from urllib.parse import unquote

import pytest
from openpyxl import load_workbook

from tests.test_job_reports import report, resource
from tests.test_job_reports import report_api as report_fixture

report_api = report_fixture


@pytest.mark.parametrize("panel", ["dgc", "flink", "realtime", "batch", "mrs"])
def test_excel_export_is_a_real_workbook_and_matches_panel(mem_db, report_api, panel):
    client, _, _ = report_api
    resource(mem_db, "dgc_realtime_job", "RUNNING", attrs={"engine": "Flink", "nodes": [
        {"name": "source", "status": "RUNNING"}, {"name": "sink", "status": "EXCEPTION"}]},
        metrics={"nodes": 2, "bad_nodes": 1})
    resource(mem_db, "dgc_batch_job", "success", attrs={"instance_id": "000123", "plan_time": 1792886400000,
        "start_time": 1792886460000, "end_time": 1792886520000}, metrics={"run_minutes": 1})
    resource(mem_db, "mrs_job", "RUNNING", attrs={"job_type": "Flink", "job_state": "RUNNING",
        "job_result": "UNDEFINED", "cluster": "MRS 1", "queue": "default"}, metrics={"elapsed_min": 0})
    response = client.get(f"/api/v1/job-reports/{panel}/export")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert unquote(response.headers["content-disposition"]).endswith(".xlsx")
    assert response.content.startswith(b"PK")
    wb = load_workbook(BytesIO(response.content))
    assert wb.sheetnames == ["汇总", "作业明细", "节点状态", "运行指标"]
    expected = report(client, panel)
    ws = wb["作业明细"]
    assert ws.max_row == expected["total"] + 1
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref == f"A1:AN{ws.max_row}"
    headers = [cell.value for cell in ws[1]]
    values = [dict(zip(headers, row, strict=True)) for row in ws.iter_rows(min_row=2, values_only=True)]
    assert {row["资源主键"] for row in values} == {row["id"] for row in expected["items"]}
    for row in values:
        assert isinstance(row["最后观测时间"], datetime)
        if row["作业类别"] == "DGC 批作业":
            assert row["实例 ID"] == "000123"
            assert isinstance(row["开始时间"], datetime)
            assert row["运行时长（分钟）"] == 1
        if row["作业类别"] == "MRS 作业":
            assert row["执行状态"] == "RUNNING" and row["执行结果"] == "UNDEFINED"
            assert row["MRS 时长（分钟）"] == 0
            assert row["运行时长（分钟）"] is None
    summary = dict(wb["汇总"].iter_rows(min_row=2, values_only=True))
    assert summary["作业数"] == expected["total"]


def test_excel_export_includes_all_matching_rows_and_enforces_scope(mem_db, report_api):
    client, _, _ = report_api
    for i in range(105):
        resource(mem_db, "dgc_batch_job", "fail", name=f"export-{i}", attrs={"engine": "Flink"})
    resource(mem_db, "dgc_batch_job", "success", name="export-unmatched")
    foreign = resource(mem_db, "dgc_batch_job", "fail", name="export-foreign", tenant_id=2, project_ref=21)
    response = client.get("/api/v1/job-reports/batch/export",
                          params={"keyword": "export-", "status": "FAILED", "engine": "flink"})
    assert response.status_code == 200
    ws = load_workbook(BytesIO(response.content))["作业明细"]
    headers = [cell.value for cell in ws[1]]
    rows = [dict(zip(headers, row, strict=True)) for row in ws.iter_rows(min_row=2, values_only=True)]
    assert len(rows) == 105 and len({row["资源主键"] for row in rows}) == 105
    assert all(row["租户 ID"] == 1 and row["资源主键"] != foreign.id for row in rows)
    no_access = client.get("/api/v1/job-reports/batch/export", headers={"X-Tenant-Id": "2"})
    assert no_access.status_code == 403
    empty = client.get("/api/v1/job-reports/batch/export", params={"tenant_id": 2})
    assert load_workbook(BytesIO(empty.content))["作业明细"].max_row == 1


def test_excel_export_keeps_text_and_missing_values(mem_db, report_api):
    client, _, _ = report_api
    resource(mem_db, "dgc_realtime_job", "RUNNING", name='=HYPERLINK("https://example.invalid")',
             attrs={"job_status": "RUNNING", "workspace": "=1+1", "private_secret": "do-not-export",
                    "nodes": [{"name": "=1+1", "status": "#REF!", "token": "do-not-export"}]},
             metrics={"run_minutes": None, "bad_nodes": 0, "custom": "=1+1"})
    response = client.get("/api/v1/job-reports/realtime/export")
    assert response.status_code == 200
    wb = load_workbook(BytesIO(response.content), data_only=False)
    assert wb["作业明细"]["A2"].data_type == "s"
    assert wb["作业明细"]["A2"].value.startswith("=HYPERLINK")
    assert wb["节点状态"]["D2"].value == "=1+1" and wb["节点状态"]["D2"].data_type == "s"
    assert wb["节点状态"]["E2"].value == "#REF!" and wb["节点状态"]["E2"].data_type == "s"
    for ws in wb:
        for row in ws:
            assert all(cell.data_type != "f" and cell.value != "do-not-export" for cell in row)
    headers = [cell.value for cell in wb["作业明细"][1]]
    row = dict(zip(headers, next(wb["作业明细"].iter_rows(min_row=2, values_only=True)), strict=True))
    assert row["运行时长（分钟）"] is None and row["异常节点数"] == 0


def test_excel_export_requires_report_permission(report_api):
    client, user, _ = report_api
    user.permissions = []
    assert client.get("/api/v1/job-reports/dgc/export").status_code == 403
