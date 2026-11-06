"""已有完成巡检报告的规范化合同；CPU/内存以报告真实内容为准。"""
from datetime import timedelta

import pytest

from app.services.gaussdb_reports import normalize_metric_series
from tests.test_gaussdb_monitoring import START, START_MS, path, workbook
from tests.test_gaussdb_monitoring import gauss_api as gauss_api


def test_normalized_api_and_export_keep_actual_cpu_memory_units_nodes_and_holes(gauss_api):
    g = gauss_api
    response = g.client.get(path("inspections/record-own"))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    cpu, memory = data["metric_series"]
    assert cpu["metric"] == "cpu" and memory["metric"] == "memory"
    assert cpu["unit"] == memory["unit"] == "%"
    assert cpu["node_name"] == "node-11" and cpu["node_ip"] == "192.0.2.11"
    assert [p["value"] for p in cpu["points"]] == [0, 75.5, None]
    assert [p["value"] for p in memory["points"]] == [23, None]
    assert cpu["points"][1]["observed_at"] == (START + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    assert (cpu["min"], cpu["max"], cpu["average"]) == (0, 75.5, 37.75)
    before = len(g.cloud.requests)
    book = workbook(g.client.get(path("export"), params={"panel": "inspection-detail", "record_id": "record-own"}))
    assert len(g.cloud.requests) == before
    assert [r[8] for r in book["巡检指标"].iter_rows(min_row=2, values_only=True)] == [0, 75.5, None, 23, None]
    overview = list(book["巡检指标概览"].iter_rows(min_row=2, values_only=True))
    assert [r[0] for r in overview] == ["cpu", "memory"]
    assert all("不代表当前使用率" in r[-1] for r in overview)
    assert data["items"][0]["indicators"][0]["metrics"][0]["data"][0]["metrics"] == [0, 75.5, None]


def test_cpu_only_report_does_not_invent_memory_or_current_values(gauss_api):
    g = gauss_api
    report = g.cloud.default("record-own", 11)
    report["inspection_results"] = report["inspection_results"][:1]
    report["inspection_results"][0]["indicators"][0]["metrics"][0]["data"][0]["time"][0] = START_MS - 86_400_000
    g.cloud.overrides["record-own"] = report
    data = g.client.get(path("inspections/record-own")).json()["data"]
    assert [s["metric"] for s in data["metric_series"]] == ["cpu"]
    assert data["metric_series"][0]["points"][0]["observed_at"] == (
        START - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    assert "不代表当前" in data["warning"]
    overview = g.client.get(path("overview")).json()["data"]
    assert overview["cpu_usage"] is overview["memory_usage"] is None


def test_distinct_nodes_units_and_summary_only_metrics_remain_separate():
    checks = [{"name": "内存", "item": "os_memory", "indicators": [
        {"name": "已用内存", "unit": "MB", "node": "fallback-node", "metrics": [
            {"name": "node-a", "ip": "192.0.2.1", "p95": 50,
             "data": {"time": [START_MS, START_MS + 60000, START_MS + 120000], "metrics": ["0", "NaN", "bad"]}},
            {"name": "node-b", "data": [{"time": [START_MS], "metrics": [-1]}]},
            {"average": 42}]},
        {"name": "内存使用率", "unit": "%", "metrics": [{"name": "node-a", "min": 0}]},
        {"name": "未知单位内存", "metrics": [{"name": "node-a", "max": float("inf")}]}]}]
    series = normalize_metric_series(checks)
    assert len(series) == 5
    assert [s.unit for s in series] == ["MB", "MB", "MB", "%", None]
    assert series[0].p95 == 50 and [p.value for p in series[0].points] == [0, None, None]
    assert series[1].points[0].value == -1  # 原生报告值不按使用率规则截断。
    assert all(s.node_name == "fallback-node" for s in series[:3])
    assert series[2].average == 42 and series[2].points == []
    assert series[4].max is None


@pytest.mark.parametrize("status", ["running", "failed", "pending"])
def test_detail_rejects_uncompleted_report_even_when_instance_matches(gauss_api, status):
    g = gauss_api
    report = g.cloud.default("record-own", 11)
    report["status"] = status
    g.cloud.overrides["record-own"] = report
    assert g.client.get(path("inspections/record-own")).status_code == 404
    assert g.client.get(path("export"), params={"panel": "inspection-detail", "record_id": "record-own"}).status_code == 404


def test_missing_detail_status_requires_membership_in_completed_records(gauss_api):
    g = gauss_api
    g.cloud.overrides["records"] = {"total_count": 1, "records": [
        {"record_id": "record-own", "instance_id": "same-cloud-instance", "status": "running"}]}
    response = g.client.get(path("inspections/record-own"))
    assert response.status_code == 404 and "CPU 使用率" not in response.text
    assert [r.url.path.rsplit("/", 1)[-1] for r in g.cloud.requests] == ["record-own", "records"]


def test_normalizer_tolerates_malformed_nested_data_and_preserves_missing_values():
    series = normalize_metric_series([{"name": "other", "indicators": [None, {"name": "队列数", "metrics": [None, {
        "data": [{"time": [True, "2026-11-01", float("inf"), START_MS]}, {"time": [START_MS + 1], "metrics": [True]}]
    }]}]}])
    assert len(series) == 1 and series[0].metric == "other" and series[0].unit is None
    assert [p.value for p in series[0].points] == [None, None]


def test_metric_unit_overrides_indicator_and_points_stably_sort_keep_nulls_and_warning(gauss_api):
    g = gauss_api
    report = g.cloud.default("record-own", 11)
    report["inspection_results"] = [{"name": "内存", "item": "os_memory", "indicators": [{
        "name": "已用内存", "unit": "MB", "metrics": [{"name": "node-a", "unit": "GB", "data": [
            {"time": [START_MS + 60000, START_MS, START_MS, START_MS + 120000, "invalid"],
             "metrics": ["NaN", 2, None, 4, 999, 123]},
            {"time": [START_MS + 180000, START_MS + 240000], "metrics": [8]}]},
            {"name": "node-b", "data": {"time": [START_MS], "metrics": [100]}},
            {"name": "node-c", "unit": "%", "data": None}]}]}]
    g.cloud.overrides["record-own"] = report
    data = g.client.get(path("inspections/record-own")).json()["data"]
    first, second, third = data["metric_series"]
    assert [first["unit"], second["unit"], third["unit"]] == ["GB", "MB", "%"]
    assert [p["value"] for p in first["points"]] == [2, None, None, 4, 8, None]
    times = [p["observed_at"] for p in first["points"]]
    assert times == sorted(times) and times[0] == times[1]  # 同时间戳原顺序保留。
    assert times[0] == START.isoformat().replace("+00:00", "Z")
    assert "丢弃无效采样时间 1 个" in data["warning"]
    assert "没有配对采样时间的数值 1 个" in data["warning"]
    assert "缺少配对数值 1 个" in data["warning"]
    assert "异常采样值 1 个" in data["warning"]
    assert data["items"][0]["indicators"][0]["unit"] == "MB"
    assert data["items"][0]["indicators"][0]["metrics"][0]["data"][0]["time"][-1] == "invalid"
    before = len(g.cloud.requests)
    book = workbook(g.client.get(path("export"), params={"panel": "inspection-detail", "record_id": "record-own"}))
    assert len(g.cloud.requests) == before
    rows = list(book["巡检指标"].iter_rows(min_row=2, values_only=True))
    assert [r[4] for r in rows] == ["GB"] * 6 + ["MB"]
    assert [r[8] for r in rows[:6]] == [2, None, None, 4, 8, None]
    assert any("丢弃无效采样时间" in str(c.value) for row in book["查询条件"] for c in row)
