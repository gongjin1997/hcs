"""作业定义 / 原生执行 / 节点 / 独立实例的报表与 Excel 交叉回归。"""

from datetime import datetime
from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.core.deps import scope_of
from app.models import InspectionRun
from app.models.base import utcnow
from app.reports.job_excel import job_workbook
from app.services.job_report_service import JobReportFilters, _filtered_snapshot, get_job_report
from tests.test_job_reports import report, resource
from tests.test_job_reports import report_api as report_fixture

report_api = report_fixture


def scoped_report(db, api, panel="dgc", **filters):
    return get_job_report(db, scope_of(api[1], None), panel, JobReportFilters(**filters))


def test_definitions_survive_empty_or_failed_instance_queries_and_exclude_instances(mem_db, report_api):
    definitions = [resource(mem_db, "dgc_batch_job", "success", attrs={
        "definition_present": True, "data_object": "DEFINITION", "source": "dataarts_definition",
        "job_type": "BATCH", "schedule_status": "NORMAL", "instance_status": None,
        "instance_query_status": state, "listed_last_instance_status": "success",
        "listed_last_instance_end_time": 1792800000000,
    }) for state in ("EMPTY", "FAILED")]
    legacy = resource(mem_db, "dgc_batch_job", "success")
    instance = resource(mem_db, "dgc_batch_job", "success", attrs={
        "definition_present": False, "data_object": "INSTANCE", "source": "dataarts_instance",
        "instance_status": "success", "instance_id": "orphan-1",
    })
    realtime = resource(mem_db, attrs={"definition_present": True, "job_status": None})
    for panel in ("dgc", "batch"):
        data = scoped_report(mem_db, report_api, panel)
        expected = {row.id for row in definitions} | {legacy.id} | ({realtime.id} if panel == "dgc" else set())
        assert {row.id for row in data.items} == expected
        assert data.summary.status_counts["SUCCEEDED"] == 1
        assert all(row.object_level == "DEFINITION" for row in data.items)
        assert data.summary.by_collection_source == {"dataarts_definition": len(expected)}
    for filters in ({"object_level": "INSTANCE"}, {"source": "dataarts_instance"}):
        data = scoped_report(mem_db, report_api, "batch", **filters)
        assert [row.id for row in data.items] == [instance.id]
        assert data.summary.by_object_level == {"INSTANCE": 1}
    assert scoped_report(mem_db, report_api, "batch", object_level="INSTANCE", source="dataarts_definition").total == 0


@pytest.mark.parametrize("evidence", [
    {"engine": "Flink"}, {"engines": ["Flink"]}, {"engines": ["Spark", "Flink"]},
    {"job_type": "Flink"}, {"single_node_type": "MRSFlinkJob"}, {"node_types": ["MRSSpark", "MRSFlinkJob"]},
    {"definition": {"engines": ["Flink"]}}, {"definition": {"single_node_type": "MRSFlinkJob"}},
])
def test_flink_requires_exact_engine_evidence_and_never_mixes_parent_and_child(mem_db, report_api, evidence):
    definition = resource(mem_db, attrs={"job_type": "REAL_TIME", **evidence})
    resource(mem_db, name="flink-name-only", attrs={"job_type": "REAL_TIME"})
    resource(mem_db, attrs={"engines": ["not-flink", "FlinkSQLCustom"]})
    native = resource(mem_db, "mrs_job", "RUNNING", attrs={
        "source": "mrs_job_execution", "job_type": "Flink", "job_state": "RUNNING", "job_id": "native-1"})
    node = resource(mem_db, "dgc_mrs_node", "running", attrs={
        "source": "dataarts_instance_detail", "node_type": "MRSFlinkJob", "node_status": "running"})
    assert [row.id for row in scoped_report(mem_db, report_api, "flink").items] == [definition.id]
    assert [row.id for row in scoped_report(mem_db, report_api, "flink", engine="flink").items] == [definition.id]
    assert [row.id for row in scoped_report(mem_db, report_api, "flink", object_level="EXECUTION").items] == [native.id]
    assert [row.id for row in scoped_report(mem_db, report_api, "flink", object_level="NODE").items] == [node.id]
    assert [row.id for row in scoped_report(mem_db, report_api, "mrs").items] == [native.id]
    assert [row.id for row in scoped_report(mem_db, report_api, "mrs", resource_type="dgc_mrs_node").items] == [node.id]


def test_legacy_mrs_nodes_cannot_inflate_native_counts(mem_db, report_api):
    legacy = resource(mem_db, "mrs_job", "success", attrs={"node_type": "MRSSpark", "node_status": "success"})
    native = resource(mem_db, "mrs_job", "FINISHED", attrs={
        "source": "mrs_job_execution", "job_state": "FINISHED", "job_result": "SUCCEEDED"})
    assert [row.id for row in scoped_report(mem_db, report_api, "mrs").items] == [native.id]
    nodes = scoped_report(mem_db, report_api, "mrs", object_level="NODE")
    assert nodes.items[0].id == legacy.id and nodes.items[0].resource_type == "dgc_mrs_node"
    assert nodes.items[0].execution_state == "success" and nodes.items[0].display_status == "SUCCEEDED"


@pytest.mark.parametrize(("state", "result", "display", "unknown"), [
    ("FINISHED", "SUCCEEDED", "SUCCEEDED", 0), ("FINISHED", "FAILED", "FAILED", 0),
    ("FINISHED", "KILLED", "KILLED", 0), ("FINISHED", "UNDEFINED", "FINISHED", 1),
    ("FINISHED", None, "FINISHED", 1), ("FINISHED", "CUSTOM_RESULT", "FINISHED", 1),
    ("RUNNING", "SUCCEEDED", "RUNNING", 0), ("NEW_SAVING", "UNDEFINED", "NEW_SAVING", 0),
    ("FUTURE_STATE", "SUCCEEDED", "FUTURE_STATE", 1), (None, "SUCCEEDED", "NO_DATA", 0),
    ("SUCCEEDED", "SUCCEEDED", "UNRECOGNIZED:SUCCEEDED", 1),
])
def test_native_state_and_result_are_independent(mem_db, report_api, state, result, display, unknown):
    resource(mem_db, "mrs_job", "SUCCEEDED", attrs={
        "source": "mrs_job_execution", "job_state": state, "job_result": result,
        "cluster": "cluster", "cluster_id": "c1", "queue": "q1", "job_id": "0001",
    })
    data = scoped_report(mem_db, report_api, "mrs")
    row = data.items[0]
    assert (row.execution_state, row.execution_result, row.display_status) == (state, result, display)
    assert data.summary.unknown_total == unknown
    assert data.summary.status_counts.get("SUCCEEDED", 0) == int(state == "FINISHED" and result == "SUCCEEDED")
    assert row.status_details["queue"] == "q1" and row.status_details["job_id"] == "0001"


def test_layer_filters_and_alarm_counts_cannot_cross_tenants_or_projects(mem_db, report_api):
    own = resource(mem_db, "dgc_batch_job", attrs={"data_object": "INSTANCE", "definition_present": False})
    resource(mem_db, "dgc_batch_job", tenant_id=2, project_ref=21,
             attrs={"data_object": "INSTANCE", "definition_present": False})
    assert [row.id for row in scoped_report(mem_db, report_api, "dgc", object_level="INSTANCE").items] == [own.id]
    for filters in ({"tenant_id": 2}, {"project_ref": 21}):
        data = scoped_report(mem_db, report_api, "dgc", object_level="INSTANCE", **filters)
        assert data.total == 0 and data.collection_status.state == "NOT_COLLECTED"


def test_empty_collection_status_is_scoped_and_does_not_treat_zero_as_cloud_empty(mem_db, report_api):
    assert scoped_report(mem_db, report_api, "batch").collection_status.state == "NOT_COLLECTED"
    foreign = InspectionRun(task_code="dgc_batch_instance", tenant_id=2, project_ref=21,
                            status="FAILED", error_summary="secret-foreign-credential")
    mem_db.add(foreign)
    mem_db.commit()
    assert scoped_report(mem_db, report_api, "batch").collection_status.state == "NOT_COLLECTED"
    own = InspectionRun(task_code="dgc_batch_instance", tenant_id=1, project_ref=11,
                        status="SUCCESS", resource_total=0, finished_at=utcnow())
    mem_db.add(own)
    mem_db.commit()
    assert scoped_report(mem_db, report_api, "batch", project_ref=11).collection_status.state == "UNKNOWN"
    own.stats = {"job_report_collection": {"state": "EMPTY", "complete": True, "object_level": "DEFINITION"}}
    mem_db.commit()
    assert scoped_report(mem_db, report_api, "batch", project_ref=11).collection_status.state == "CLOUD_EMPTY"
    assert scoped_report(mem_db, report_api, "batch").collection_status.state == "UNKNOWN"
    unmatched = scoped_report(mem_db, report_api, "batch", project_ref=11, object_level="INSTANCE")
    assert unmatched.collection_status.state == "UNKNOWN"
    own.status = "FAILED"
    own.error_summary = "secret-own-credential"
    mem_db.commit()
    data = scoped_report(mem_db, report_api, "batch", project_ref=11)
    assert data.collection_status.state == "FAILED"
    assert "secret" not in data.model_dump_json()
    assert foreign.status == "FAILED" and own.status == "FAILED"  # 只读，不自动启用或重试。
    resource(mem_db, "dgc_batch_job")
    assert scoped_report(mem_db, report_api, "batch", keyword="no-match").collection_status.state == "NO_MATCH"


def workbook_rows(content):
    wb = load_workbook(BytesIO(content))
    headers = [cell.value for cell in wb["作业明细"][1]]
    return wb, [dict(zip(headers, row, strict=True)) for row in wb["作业明细"].iter_rows(min_row=2, values_only=True)]


def test_excel_keeps_layers_native_fields_unknown_units_and_definition_provenance(mem_db, report_api):
    native = resource(mem_db, "mrs_job", "FINISHED", attrs={
        "source": "mrs_job_execution", "job_type": "Flink", "job_state": "FINISHED", "job_result": "UNDEFINED",
        "job_id": "000007", "cluster": "cluster", "cluster_id": "cid", "queue_name": "queue",
        "submitted_time": 1792800000000, "start_time": 1792800001000, "end_time": 1792800002000,
        "elapsed_time": 17, "elapsed_time_unit": "UNKNOWN", "private_secret": "not-for-export",
    }, metrics={"run_minutes": None})
    resource(mem_db, "dgc_mrs_node", "success", attrs={"node_type": "MRSFlinkJob", "node_status": "success"})
    resource(mem_db, "dgc_batch_job", "success", attrs={"definition_present": True, "instance_status": None,
        "instance_query_status": "EMPTY", "schedule_status": "NORMAL"})
    instance = resource(mem_db, "dgc_batch_job", "success", attrs={
        "definition_present": False, "data_object": "INSTANCE", "instance_status": "success"})
    scope = scope_of(report_api[1], None)
    wb, rows = workbook_rows(job_workbook(mem_db, scope, "mrs", JobReportFilters()))
    assert len(rows) == 1 and rows[0]["资源主键"] == native.id
    assert rows[0]["执行 ID"] == "000007" and rows[0]["队列"] == "queue"
    assert rows[0]["执行结果"] == "UNDEFINED" and rows[0]["作业状态"] != "成功"
    assert rows[0]["运行时长（分钟）"] is None and rows[0]["耗时单位"] == "UNKNOWN"
    assert isinstance(rows[0]["提交时间"], datetime)
    assert "not-for-export" not in str([[cell.value for row in ws for cell in row] for ws in wb])
    _, definitions = workbook_rows(job_workbook(mem_db, scope, "batch", JobReportFilters()))
    assert len(definitions) == 1 and definitions[0]["实例查询状态"] == "EMPTY"
    _, instances = workbook_rows(job_workbook(mem_db, scope, "batch", JobReportFilters(object_level="INSTANCE")))
    assert len(instances) == 1 and instances[0]["资源主键"] == instance.id


def test_postgres_compiles_exact_array_evidence_and_layer_filters(report_api):
    q = _filtered_snapshot(scope_of(report_api[1], None), "flink", JobReportFilters(object_level="NODE"), "postgresql")
    sql = str(select(q).compile(dialect=postgresql.dialect()))
    assert "jsonb_array_elements_text" in sql and "JSONB" in sql


def test_five_existing_report_urls_stay_compatible(report_api):
    for panel in ("dgc", "flink", "realtime", "batch", "mrs"):
        data = report(report_api[0], panel)
        assert data["panel"] == panel and data["collection_status"]["state"] == "NOT_COLLECTED"


@pytest.mark.parametrize(("reason", "expected"), [
    ("DEFAULT_WORKSPACE_EMPTY", "CLOUD_EMPTY"), ("EMPTY_DEFINITIONS", "CLOUD_EMPTY"),
    ("SCOPE_FILTERED", "NO_MATCH"), ("WORKSPACE_EXCLUDED", "NO_MATCH"),
    ("LIST_FAILED", "FAILED"), ("INSTANCE_QUERY_FAILED", "FAILED"),
    ("NO_RECENT_INSTANCES", "UNKNOWN"), ("AVAILABLE", "NO_MATCH"),
])
def test_dataarts_empty_diagnostics_use_actual_scoped_collection_reason(mem_db, report_api, reason, expected):
    mem_db.add(InspectionRun(task_code="dgc_batch_instance", tenant_id=1, project_ref=11, status="SUCCESS",
        stats={"dataarts": {"workspaces": [{"workspace": "", "workspace_mode": "default", "job_type": "BATCH",
            "reason": reason, "definitions": 0, "in_scope": 0, "error": "never-return-this-secret"}]}}))
    mem_db.add(InspectionRun(task_code="dgc_batch_instance", tenant_id=2, project_ref=21, status="FAILED",
        started_at=utcnow(), stats={"dataarts": {"workspaces": [{"job_type": "BATCH", "reason": "LIST_FAILED"}]}}))
    mem_db.commit()
    data = scoped_report(mem_db, report_api, "batch", project_ref=11)
    assert data.collection_status.state == expected
    assert data.collection_status.reasons == {reason: 1}
    assert "secret" not in data.model_dump_json()
    if expected == "CLOUD_EMPTY":
        assert scoped_report(mem_db, report_api, "batch").collection_status.state == "UNKNOWN"


def test_unknown_native_duration_cannot_reuse_zero_and_missing_contract_state_cannot_reuse_success(mem_db, report_api):
    resource(mem_db, "mrs_job", "FINISHED", attrs={"source": "mrs_job_execution", "job_state": "FINISHED",
        "job_result": "UNDEFINED", "elapsed_time": 99, "elapsed_time_unit": None}, metrics={"run_minutes": 0})
    native = scoped_report(mem_db, report_api, "mrs").items[0]
    assert native.metrics["run_minutes"] is None
    resource(mem_db, "dgc_batch_job", "success", attrs={"source": "dataarts_definition", "definition_present": True})
    assert scoped_report(mem_db, report_api, "batch").items[0].display_status == "NO_DATA"
