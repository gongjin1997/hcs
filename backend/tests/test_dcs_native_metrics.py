"""DCS V2 单位与真实响应时间回归；仅合成数据。"""
import io
from datetime import UTC, datetime, timedelta

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.collectors.base import Observation, RunContext
from app.collectors.dcs import DcsCollector
from app.metrics.dcs_samples import metric_values
from app.models import CollectSwitch
from app.models.dcs import DcsMemorySample, DcsMetricSample
from app.services import cleanup
from app.services.dcs_monitoring import native_current
from app.services.pipeline import process_observations
from tests.test_dcs_history_api import dcs_api as dcs_api
from tests.test_dcs_history_api import params, resource

AT = datetime(2026, 11, 2, tzinfo=UTC)


def test_v2_statistics_preserve_native_units_and_response_time(monkeypatch):
    monkeypatch.setattr("app.collectors.dcs.utcnow", lambda: AT, raising=False)
    calls = []

    class Client:
        def get(self, service, path, **kwargs):
            calls.append((service, path))
            if path.endswith("/statistic"):
                return {"statistics": [{"instance_id": "cache", "used_cpu": "12000.56", "input_kbps": "1.25",
                    "output_kbps": "2.5", "keys": 0, "cmd_get_count": 42, "cmd_set_count": 7,
                    "used_memory": 200, "max_memory": 1000}]}
            return {"instance_num": 1, "instances": [{"instance_id": "cache", "name": "cache", "status": "RUNNING"}]}

    rows = DcsCollector().collect(RunContext(client=Client(), account="test", project_id="test"))
    current = next(row for row in rows if row.check_item == "instance_status")
    assert current.metrics["used_cpu_seconds"] == 12000.56
    assert current.metrics["input_kbps"] == 1.25
    assert current.metrics["keys"] == 0
    assert current.metrics["cmd_get_count"] == 42
    assert current.metrics["cpu_usage"] is None
    assert current.metric_observed_at == AT
    assert current.attrs["dcs_monitoring"]["api_version"] == "v2"
    assert calls == [("dcs", "/v2/{project_id}/instances"), ("dcs", "/v2/{project_id}/instances/statistic")]



def metadata(at=AT, **extra):
    return {"metrics_source": "DCS", "dcs_monitoring": {
        "source": "DCS", "api_version": "v2", "endpoint": "instances/statistic",
        "observed_at": at.isoformat(), "time_basis": "RESPONSE_RECEIVED", "stale_after_sec": 600, **extra}}


def metric_sample(db, row, minute=1, **extra):
    point = DcsMetricSample(resource_ref=row.id, tenant_id=row.tenant_id, project_ref=row.project_ref,
        observed_at=AT + timedelta(minutes=minute), collected_at=AT + timedelta(minutes=minute, seconds=10),
        source="DCS", api_version="v2", endpoint="instances/statistic", **extra)
    db.add(point)
    db.commit()
    return point


def test_real_response_time_is_used_for_both_sample_tables_and_retry_is_idempotent(mem_db):
    at = AT - timedelta(seconds=20)
    values = {"memory_usage": 20, "used_memory_mb": 200, "max_memory_mb": 1000,
              "used_cpu_seconds": 12000.56, "input_kbps": 0, "keys": 0}
    observations = [Observation("dcs_instance", "same", "same", "instance_status", {},
        metrics=values, attrs=metadata(at), metric_observed_at=at),
        Observation("dcs_instance", "same", "same", "memory", {}, metrics={key: values[key]
            for key in ("memory_usage", "used_memory_mb", "max_memory_mb")}, metric_observed_at=at)]
    ctx = RunContext(client=None, account="1", project_id="11", tenant_id=1, project_ref=11)
    process_observations(mem_db, "dcs", observations, ctx, AT)
    process_observations(mem_db, "dcs", observations, ctx, AT + timedelta(seconds=1))
    native, = mem_db.scalars(select(DcsMetricSample)).all()
    memory, = mem_db.scalars(select(DcsMemorySample)).all()
    assert native.observed_at == memory.observed_at == at
    assert native.collected_at == AT
    assert (native.used_cpu_seconds, native.input_kbps, native.keys) == (12000.56, 0, 0)
    assert native.source == "DCS" and native.api_version == "v2"


@pytest.mark.parametrize("invalid", [None, True, "nan", "inf", -1, "-Infinity"])
def test_invalid_values_stay_null_and_do_not_erase_real_zero(invalid):
    values = metric_values({"used_cpu_seconds": invalid, "input_kbps": invalid, "keys": invalid, "cmd_get_count": 0})
    assert values["used_cpu_seconds"] is values["input_kbps"] is values["keys"] is None
    assert values["cmd_get_count"] == 0


def test_count_validation_preserves_integer_long_and_rejects_fractional_or_overflow():
    assert metric_values({"keys": "9223372036854775807"})["keys"] == 9223372036854775807
    assert metric_values({"keys": "1.5"})["keys"] is None
    assert metric_values({"keys": "9223372036854775808"})["keys"] is None


@pytest.mark.parametrize("at,state", [(AT, "FRESH"), (AT - timedelta(seconds=600), "FRESH"),
    (AT - timedelta(seconds=601), "STALE"), (AT + timedelta(seconds=31), "NO_DATA")])
def test_current_freshness_uses_real_sample_time_not_resource_last_seen(mem_db, at, state):
    row = resource(mem_db, attrs=metadata(at), metrics={"used_cpu_seconds": 1234, "memory_usage": 0}, last_seen=AT)
    current = native_current(row, AT)
    assert current["freshness"] == state
    assert current["metrics_observed_at"] == at
    assert current["used_cpu_seconds"] == (1234 if state == "FRESH" else None)
    assert current["memory_usage"] == (0 if state == "FRESH" else None)


@pytest.mark.parametrize("attrs", [{}, {"metrics_source": "AOM"}, {"metrics_source": "CES"},
    metadata(api_version="v1"), metadata(time_basis="UNVERIFIED")])
def test_old_or_unverified_snapshot_is_never_current(mem_db, attrs):
    row = resource(mem_db, attrs=attrs, metrics={"used_cpu_seconds": 999, "memory_usage": 80}, last_seen=AT)
    current = native_current(row, AT)
    assert current["used_cpu_seconds"] is None and current["memory_usage"] is None
    assert current["freshness"] != "FRESH"


def test_disabled_collection_does_not_persist_native_samples(mem_db):
    mem_db.add(CollectSwitch(tenant_id=1, resource_type="dcs_instance", enabled=False))
    mem_db.commit()
    observation = Observation("dcs_instance", "same", "same", "instance_status", {},
        attrs=metadata(), metrics={"used_cpu_seconds": 100}, metric_observed_at=AT)
    process_observations(mem_db, "dcs", [observation],
        RunContext(client=None, account="1", project_id="11", tenant_id=1, project_ref=11), AT)
    assert mem_db.scalar(select(func.count()).select_from(DcsMetricSample)) == 0


def test_native_history_counter_last_rate_mean_actual_time_and_gaps(mem_db, dcs_api):
    row = resource(mem_db)
    metric_sample(mem_db, row, 1, memory_usage=10, used_memory_mb=100, max_memory_mb=1000,
        used_cpu_seconds=12000, input_kbps=2, output_kbps=0, keys=42, cmd_get_count=1000, cmd_set_count=2000)
    metric_sample(mem_db, row, 3, memory_usage=20, used_memory_mb=200, max_memory_mb=2000,
        used_cpu_seconds=5, input_kbps=4, output_kbps=0, keys=0, cmd_get_count=3, cmd_set_count=4)
    response = dcs_api[0].get("/api/v1/dcs/metrics/history",
                              params=params([row], start=AT, end=AT+timedelta(minutes=10), interval=5))
    assert response.status_code == 200
    data = response.json()["data"]
    first, missing = data["series"][0]["points"]
    assert first["memory_usage"] == 15 and first["memory_max"] == 20
    assert first["input_kbps"] == 3 and first["max_memory_mb"] == 2000
    assert first["used_cpu_seconds"] == 5 and first["cmd_get_count"] == 3 and first["keys"] == 0
    assert datetime.fromisoformat(first["last_observed_at"]) == AT+timedelta(minutes=3)
    assert first["metric_sample_counts"]["used_cpu_seconds"] == 2
    assert missing["used_cpu_seconds"] is None and missing["last_observed_at"] is None and missing["sample_count"] == 0
    assert data["series"][0]["summaries"]["used_cpu_seconds"]["average"] is None
    assert data["series"][0]["summaries"]["used_cpu_seconds"]["latest"] == 5
    assert next(item for item in data["metrics"] if item["key"] == "used_cpu_seconds")["unit"] == "s"
    assert data["series"][0]["status"] == "PARTIAL"
    assert not dcs_api[3]


def test_native_excel_uses_same_rows_numeric_zero_blanks_and_safe_text(mem_db, dcs_api):
    row = resource(mem_db, name="=SUM(1,2)")
    metric_sample(mem_db, row, used_cpu_seconds=12000.56, keys=0, cmd_get_count=42)
    response = dcs_api[0].get("/api/v1/dcs/metrics/export",
                              params=params([row], start=AT, end=AT+timedelta(minutes=10), interval=5))
    assert response.status_code == 200
    workbook = load_workbook(io.BytesIO(response.content))
    assert workbook.sheetnames == ["查询条件", "指标口径", "当前统计", "原生指标"]
    sheet = workbook["原生指标"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    cpu = next(index for header, index in headers.items() if header.startswith("CPU累计时间（s"))
    count = next(index for header, index in headers.items() if header.startswith("数据条数（count"))
    assert sheet.cell(2, cpu).value == 12000.56 and sheet.cell(3, cpu).value is None
    assert sheet.cell(2, count).value == 0 and sheet.cell(2, count).data_type == "n"
    assert sheet.cell(2, 3).data_type == "s"
    conditions = str(list(workbook["查询条件"].values))
    assert "AOM/CES" in conditions
    assert "源端未提供采样时间" in conditions
    assert "本地获取时间" in [cell.value for cell in workbook["当前统计"][1]]
    assert "末次本地获取时间" in headers
    assert not dcs_api[3]


@pytest.mark.parametrize("endpoint", ["metrics/history", "metrics/export"])
def test_new_endpoints_enforce_permission_scope_query_and_no_cloud(mem_db, dcs_api, endpoint):
    client, user, _, calls = dcs_api
    own = resource(mem_db)
    foreign = resource(mem_db, tenant_id=2, project_ref=21)
    query = params([own], start=AT, end=AT+timedelta(minutes=5))
    assert client.get(f"/api/v1/dcs/{endpoint}", params=query).status_code == 200
    user.permissions = []
    assert client.get(f"/api/v1/dcs/{endpoint}", params=query).status_code == 403
    user.permissions = ["report:view"]
    assert client.get(f"/api/v1/dcs/{endpoint}", params=params([foreign])).status_code == 404
    assert client.get(f"/api/v1/dcs/{endpoint}", params=params([own], interval=2)).status_code == 400
    assert not calls


def test_mismatched_sample_project_cannot_leak_into_history(mem_db, dcs_api):
    row = resource(mem_db, resource_id="shared")
    point = metric_sample(mem_db, row, used_cpu_seconds=123)
    point.project_ref = 12
    mem_db.commit()
    response = dcs_api[0].get("/api/v1/dcs/metrics/history",
                              params=params([row], start=AT, end=AT+timedelta(minutes=5)))
    data = response.json()["data"]
    assert data["series"][0]["source"] == "NONE"
    assert data["series"][0]["points"][0]["used_cpu_seconds"] is None


def test_native_samples_cleanup_90_day_boundary_and_orphans(mem_db):
    row = resource(mem_db)
    metric_sample(mem_db, row, minute=0, used_cpu_seconds=0)
    metric_sample(mem_db, row, minute=1, used_cpu_seconds=1)
    result = cleanup.run_cleanup(mem_db, AT+timedelta(days=90, seconds=1))
    assert result["dcs_metric_sample"] == 1
    assert mem_db.scalar(select(func.count()).select_from(DcsMetricSample)) == 1
    mem_db.delete(row)
    mem_db.commit()
    cleanup.run_cleanup(mem_db, AT+timedelta(days=90, seconds=1))
    assert mem_db.scalar(select(func.count()).select_from(DcsMetricSample)) == 0
