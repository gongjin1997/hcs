"""上传 PDF 的源字段边界与本地作业报表回归；所有响应均为合成数据，不证明正式 API 可用。"""
from copy import deepcopy
from datetime import datetime
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.collectors.base import RunContext
from app.collectors.dataarts import DataArtsBatchCollector, DataArtsRealtimeCollector
from app.collectors.mrs import MrsClusterCollector
from app.services.job_report_service import _status_details
from tests.test_job_reports import report, resource
from tests.test_job_reports import report_api as report_fixture

report_api = report_fixture


class LocalClient:
    def __init__(self, respond):
        self.respond = respond
        self.requests = []

    def get(self, service, path, **kwargs):
        self.requests.append((service, path, deepcopy(kwargs)))
        return deepcopy(self.respond(service, path, kwargs))


def context(client, **params):
    return RunContext(client=client, account="synthetic", project_id="synthetic-project",
                      workspaces=["workspace-a"], params=params)


def test_equivalent_batch_instance_ids_cannot_evade_duplicate_page_detection():
    rows = [{"jobName": "batch", "instanceId": identity, "status": "success", "planTime": 1000}
            for identity in (17, "17")]
    ctx = context(LocalClient(lambda *_: {"total": 2, "instances": rows}))
    assert DataArtsBatchCollector().collect(ctx) == []
    assert ctx.errors


@pytest.mark.parametrize("state", [None, "", "FUTURE_STATE"])
def test_missing_unknown_cluster_state_is_not_a_successful_measurement(state):
    ctx = context(LocalClient(lambda *_: {"clusterTotal": "1", "clusters": [
        {"clusterId": "cluster-a", "clusterName": "A", "clusterState": state}]}))
    row = MrsClusterCollector().collect(ctx)[0]
    assert "cluster_state" not in row.facts
    assert ctx.errors


def test_missing_nodes_remains_missing_in_report_detail_contract():
    assert _status_details({"nodes": None}) == {"nodes": None}
    assert _status_details({"nodes": []}) == {"nodes": []}


def test_excel_does_not_invent_millisecond_units_for_unverified_numeric_dates(mem_db, report_api):
    client, _, _ = report_api
    resource(mem_db, "dgc_realtime_job", "RUNNING", attrs={"start_time": 1792886400, "end_time": 1792886460})
    response = client.get("/api/v1/job-reports/realtime/export")
    assert response.status_code == 200
    ws = load_workbook(BytesIO(response.content))["作业明细"]
    values = dict(zip((cell.value for cell in ws[1]), next(ws.iter_rows(min_row=2, values_only=True)), strict=True))
    assert values["开始时间"] == 1792886400
    assert values["结束时间"] == 1792886460
    assert not isinstance(values["开始时间"], datetime)


def test_explicit_missing_job_state_does_not_reuse_legacy_snapshot_timestamp(mem_db, report_api):
    client, _, _ = report_api
    resource(mem_db, "dgc_batch_job", "success", attrs={"instance_status": None}, status_since=None)
    row = report(client, "batch")["items"][0]
    assert row["display_status"] == "NO_DATA"
    assert row["status_since"] is None


def test_mrs_node_millisecond_timestamps_are_dates_in_the_workbook(mem_db, report_api):
    client, _, _ = report_api
    resource(mem_db, "mrs_job", "running", attrs={"node_status": "running", "node_type": "MRSSpark",
                                                 "start_time": 1792886400000, "end_time": 1792886460000})
    response = client.get("/api/v1/job-reports/mrs/export")
    ws = load_workbook(BytesIO(response.content))["作业明细"]
    values = dict(zip((cell.value for cell in ws[1]), next(ws.iter_rows(min_row=2, values_only=True)), strict=True))
    assert isinstance(values["开始时间"], datetime) and isinstance(values["结束时间"], datetime)
    assert values["节点类型"] == "MRSSpark"


def test_mrs_data_source_is_stated_in_report_and_workbook(mem_db, report_api):
    client, _, _ = report_api
    resource(mem_db, "mrs_job", "running", attrs={"node_status": "running"})
    data = report(client, "mrs")
    assert any("DataArts" in note and "MRS" in note for note in data["data_notes"])
    response = client.get("/api/v1/job-reports/mrs/export")
    summary = dict(load_workbook(BytesIO(response.content))["汇总"].iter_rows(min_row=2, values_only=True))
    assert "DataArts" in str(summary.get("数据说明")) and "MRS" in str(summary["数据说明"])


def test_realtime_workspaces_and_batch_missing_duration_keep_independent_facts(monkeypatch):
    now = 1793577600000
    monkeypatch.setattr("app.collectors.dataarts.time.time", lambda: now / 1000)

    def respond(service, path, kwargs):
        workspace = kwargs["headers"]["workspace"]
        if path.endswith("/jobs"):
            return {"total": 1, "jobs": [{"name": "same", "jobType": "REAL_TIME"}]}
        return {"name": "same", "status": "NORMAL" if workspace == "workspace-a" else "EXCEPTION"}

    ctx = context(LocalClient(respond))
    ctx.workspaces = ["workspace-a", "workspace-b"]
    rows = DataArtsRealtimeCollector().collect(ctx)
    assert {row.resource_id: row.facts for row in rows} == {
        "workspace-a/same": {"status": "NORMAL"}, "workspace-b/same": {"status": "EXCEPTION"}}
    assert all(row.metrics["bad_nodes"] is None for row in rows)


@pytest.mark.parametrize("kind,panel,field,raw", [
    ("dgc_realtime_job", "realtime", "job_status", "success"),
    ("dgc_realtime_job", "realtime", "job_status", "RUNNING"),
    ("dgc_realtime_job", "realtime", "job_status", "SUCCEEDED"),
    ("dgc_batch_job", "batch", "instance_status", "succeeded"),
    ("dgc_batch_job", "batch", "instance_status", "SUCCEEDED"),
    ("dgc_batch_job", "batch", "instance_status", "FAILED"),
])
def test_current_dataarts_snapshot_cannot_promote_undocumented_alias_to_success(
        mem_db, report_api, kind, panel, field, raw):
    client, _, _ = report_api
    resource(mem_db, kind, raw, attrs={field: raw})
    data = report(client, panel)
    row = data["items"][0]
    assert row["status_known"] is False
    assert row["status_raw"] == raw and row["execution_state"] == raw
    assert data["summary"]["unknown_total"] == 1
    assert not set(data["summary"]["status_counts"]) & {"RUNNING", "SUCCEEDED", "FAILED"}
    response = client.get(f"/api/v1/job-reports/{panel}/export")
    summary = dict(load_workbook(BytesIO(response.content))["汇总"].iter_rows(min_row=2, values_only=True))
    assert "成功" not in summary and "失败" not in summary and "运行中" not in summary


def test_batch_submit_time_reaches_report_and_excel_with_pdf_millisecond_unit(mem_db, report_api):
    client, _, _ = report_api
    source = {"jobName": "batch", "instanceId": 1, "status": "success", "planTime": 1792886400000,
              "submitTime": 1792886401000, "instanceType": 5, "startTime": 1792886402000,
              "endTime": 1792886403000}
    ctx = context(LocalClient(lambda *_: {"total": 1, "instances": [source]}))
    observation = DataArtsBatchCollector().collect(ctx)[0]
    resource(mem_db, observation.resource_type, observation.status_raw,
             attrs=observation.attrs, metrics=observation.metrics)
    row = report(client, "batch")["items"][0]
    assert row["status_details"]["submitted_time"] == source["submitTime"]
    assert row["status_details"]["instance_type"] == 5
    response = client.get("/api/v1/job-reports/batch/export")
    ws = load_workbook(BytesIO(response.content))["作业明细"]
    values = dict(zip((cell.value for cell in ws[1]), next(ws.iter_rows(min_row=2, values_only=True)), strict=True))
    assert isinstance(values["提交时间"], datetime)


def test_legacy_submit_time_is_preserved_without_overriding_explicit_missing_current_value():
    assert _status_details({"submit_time": 1234})["submitted_time"] == 1234
    assert _status_details({"submit_time": 1234, "submitted_time": None})["submitted_time"] is None


def test_workbook_can_export_a_current_snapshot_with_unknown_nodes(mem_db, report_api):
    client, _, _ = report_api
    resource(mem_db, attrs={"nodes": None}, metrics={"nodes": None, "bad_nodes": None})
    response = client.get("/api/v1/job-reports/realtime/export")
    assert response.status_code == 200
    wb = load_workbook(BytesIO(response.content))
    assert wb["节点状态"].max_row == 1
    ws = wb["作业明细"]
    values = dict(zip((cell.value for cell in ws[1]), next(ws.iter_rows(min_row=2, values_only=True)), strict=True))
    assert values["节点数"] is None and values["异常节点数"] is None


@pytest.mark.parametrize("metadata,health,expected", [
    (None, "OK", "UNKNOWN"),
    ({"checked_rules": 0, "unavailable_rules": 0}, "OK", "UNKNOWN"),
    ({"checked_rules": 1, "unavailable_rules": 1}, "OK", "UNKNOWN"),
    ({"checked_rules": 1, "unavailable_rules": 0, "age_seconds": 901}, "OK", "UNKNOWN"),
    ({"checked_rules": 1, "unavailable_rules": 0}, "OK", "OK"),
    (None, "MAJOR", "MAJOR"),
])
def test_job_list_and_workbook_use_effective_inspection_health(mem_db, report_api, metadata, health, expected):
    from datetime import timedelta

    from app.models.base import utcnow

    client, _, _ = report_api
    attrs = {"instance_status": "running"}
    if metadata is not None:
        values = dict(metadata)
        values["evaluated_at"] = (utcnow() - timedelta(seconds=values.pop("age_seconds", 0))).isoformat()
        values.update(stale_after_sec=900, task_code="dgc_batch_instance")
        attrs["inspection_monitoring"] = values
    saved = resource(mem_db, "dgc_batch_job", "running", attrs=attrs, health=health)
    data = report(client, "batch")
    assert data["items"][0]["health"] == expected
    response = client.get("/api/v1/job-reports/batch/export")
    ws = load_workbook(BytesIO(response.content))["作业明细"]
    values = dict(zip((cell.value for cell in ws[1]), next(ws.iter_rows(min_row=2, values_only=True)), strict=True))
    assert values["巡检健康度"] == expected
    assert saved.health == health  # 查询不把派生健康度回写持久化快照。
