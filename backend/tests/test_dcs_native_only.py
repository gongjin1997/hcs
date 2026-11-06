from types import SimpleNamespace

import pytest

from app.collectors.base import RunContext
from app.collectors.dcs import DcsCollector
from app.core.config import get_settings
from app.hcs import HcsError


def collect(values, *, params=None, source="CES"):
    calls = []

    class Client:
        def get(self, service, path, **kwargs):
            calls.append((service, path))
            assert service == "dcs"
            if path == "/v2/{project_id}/instances/statistic":
                return {"statistics": [{"instance_id": "one", **values}]}
            assert path == "/v2/{project_id}/instances"
            return {"instance_num": 1, "instances": [{"instance_id": "one", "name": "one", "status": "RUNNING", **values}]}

    ctx = RunContext(client=Client(), account="test", project_id="project",
                     params=params or {}, tenant=SimpleNamespace(metrics_source=source, endpoints={"aom": "https://aom.invalid"},
                                           capabilities={"ces": {"available": True}}))
    return DcsCollector().collect(ctx), calls


def test_dcs_reads_only_native_api_even_when_legacy_tenant_says_ces(monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_ces_enabled", True)
    rows, calls = collect({"used_memory": "200", "max_memory": "1000", "cpu_usage": 12.5,
                          "connected_clients": "7", "max_clients": 100})
    assert calls == [("dcs", "/v2/{project_id}/instances"), ("dcs", "/v2/{project_id}/instances/statistic")]
    facts = {r.check_item: r.facts for r in rows}
    assert facts["memory"] == {"memory_usage": 20, "used_memory_mb": 200, "max_memory_mb": 1000}
    assert "cpu" not in facts and "connections" not in facts


@pytest.mark.parametrize("values", [{}, {"used_memory": None, "max_memory": 100},
                                    {"used_memory": 10, "max_memory": 0},
                                    {"used_memory": "nan", "max_memory": 100},
                                    {"used_memory": -1, "max_memory": 100},
                                    {"used_memory": True, "max_memory": 100}])
def test_missing_or_invalid_native_metrics_are_not_zero_filled(values):
    rows, _ = collect(values)
    assert [r.check_item for r in rows] == ["instance_status", "memory"]
    assert rows[1].facts["memory_usage"] is None  # 显式缺样中断连续命中，不伪造0或恢复。


def test_real_zero_native_memory_remains_zero():
    rows, _ = collect({"used_memory": 0, "max_memory": 100})
    assert next(r for r in rows if r.check_item == "memory").facts["memory_usage"] == 0


def test_native_v2_cumulative_cpu_seconds_are_not_percent():
    rows, _ = collect({"used_memory": 200, "max_memory": 1000, "used_cpu": "12000.56"})
    assert not any(row.check_item == "cpu" for row in rows)


@pytest.mark.parametrize("statistics", [{"used_memory": 200}, {"max_memory": 1000}, {}])
def test_statistics_does_not_mix_missing_memory_fields_with_list_snapshot(statistics):
    class Client:
        def get(self, _service, path, **_kwargs):
            if path.endswith("/statistic"):
                return {"statistics": [{"instance_id": "one", **statistics}]}
            return {"instance_num": 1, "instances": [{"instance_id": "one", "name": "one", "status": "RUNNING",
                                                      "used_memory": 10, "max_memory": 100}]}
    ctx = RunContext(client=Client(), account="test", project_id="project")
    rows = DcsCollector().collect(ctx)
    assert [row.check_item for row in rows] == ["instance_status", "memory"]
    assert rows[1].facts["memory_usage"] is None
    assert rows[0].metrics["memory_usage"] is None


def test_native_statistics_failure_keeps_real_list_fields_and_records_partial_failure():
    class Client:
        def get(self, _service, path, **_kwargs):
            if path.endswith("/statistic"):
                raise HcsError(503, "DCS.SERVICE_UNAVAILABLE", "synthetic failure")
            return {"instance_num": 1, "instances": [{"instance_id": "one", "name": "one", "status": "RUNNING",
                                                      "used_memory": 10, "max_memory": 100}]}
    ctx = RunContext(client=Client(), account="test", project_id="project")
    rows = DcsCollector().collect(ctx)
    assert next(row for row in rows if row.check_item == "memory").facts["memory_usage"] == 10
    assert ctx.errors


def test_no_running_instances_never_queries_statistics_or_cloud_metrics():
    calls = []
    class Client:
        def get(self, service, path, **_kwargs):
            calls.append((service, path))
            assert service == "dcs" and path == "/v2/{project_id}/instances"
            return {"instance_num": 1, "instances": [{"instance_id": "one", "name": "one", "status": "STOPPED"}]}
    rows = DcsCollector().collect(RunContext(client=Client(), account="test", project_id="project"))
    assert len(calls) == 1
    assert [row.check_item for row in rows] == ["instance_status", "memory"]
    assert rows[1].facts["memory_usage"] is None


@pytest.mark.parametrize("source", ["AUTO", "AOM", "CES", "PROMETHEUS", "NONE"])
def test_legacy_aom_metrics_cannot_trigger_extra_dcs_collection_requests(source):
    rows, calls = collect({"used_memory": 200, "max_memory": 1000, "used_cpu": "12000.56"},
                          source=source, params={"aom": {"namespace": "CUSTOM.DcsTest", "dimension": "cache_id",
                          "metrics": {"memory_usage": "memory_percent", "cpu_usage": "cpu_percent",
                                      "connected_clients": "client_count"}}})
    assert calls == [("dcs", "/v2/{project_id}/instances"), ("dcs", "/v2/{project_id}/instances/statistic")]
    assert [r.check_item for r in rows] == ["instance_status", "memory"]
    assert rows[1].facts["memory_usage"] == 20
    assert rows[0].metrics["cpu_usage"] is rows[0].metrics["connected_clients"] is None
