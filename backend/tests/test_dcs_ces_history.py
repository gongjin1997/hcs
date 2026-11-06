"""旧 DCS/CES/AOM 配置不改变原生采样；文件名保留兼容既有测试入口。"""
from datetime import timedelta

import pytest

from app.metrics import dcs_history, dcs_samples
from app.metrics.dcs_samples import RawMemoryPoint
from app.models import HcsAccount
from app.services import dcs_history_service as service

from .test_dcs_history_api import AOM_MAPPING, START, enable_aom, install_aom, query, resource, sample
from .test_dcs_history_api import dcs_api as dcs_api


def test_retired_ces_adapter_only_reexports_pure_native_sample_helpers():
    assert dcs_history.RawMemoryPoint is dcs_samples.RawMemoryPoint
    assert dcs_history.finite_number is dcs_samples.finite_number
    assert set(dcs_history.__all__) == {"RawMemoryPoint", "finite_number"}
    assert not hasattr(dcs_history, "query_memory_history")
    assert not hasattr(service, "query_memory_history")
    assert not hasattr(service, "_ces_history")
    assert not hasattr(dcs_history, "HcsClient")


@pytest.mark.parametrize("dimension", ["cache_id", "memcached_cache_id"])
def test_legacy_mapping_does_not_replace_native_redis_or_memcached_samples(mem_db, dcs_api, monkeypatch, dimension):
    row = resource(mem_db, attrs={"engine": "Memcached" if dimension.startswith("memcached") else "Redis"})
    sample(mem_db, row, 0, 40, used=400, capacity=1000)
    sample(mem_db, row, 5, 60, used=600, capacity=1000)
    cloud = install_aom(mem_db, monkeypatch, [RawMemoryPoint(START, 99, 100)],
                        mapping={**AOM_MAPPING, "dimension": dimension})
    data = query(dcs_api[0], [row], end=START + timedelta(minutes=10), interval=5)["series"][0]
    assert data["source"] == "LOCAL" and data["status"] == "AVAILABLE"
    assert [p["memory_usage"] for p in data["points"]] == [40, 60]
    assert [p["memory_max"] for p in data["points"]] == [40, 60]
    assert data["summary"]["peak_usage"] == 60 and cloud.calls == dcs_api[3] == []


def test_seven_days_twenty_instances_keep_every_actual_native_point(mem_db, dcs_api):
    from app.models.dcs import DcsMemorySample

    rows = [resource(mem_db, resource_id=f"cache-{i}") for i in range(20)]
    count = 7 * 24 * 12
    mem_db.add_all([DcsMemorySample(resource_ref=row.id, tenant_id=row.tenant_id, project_ref=row.project_ref,
                                  observed_at=START + timedelta(minutes=5 * i), memory_usage=i % 75,
                                  used_memory_mb=i % 75, max_memory_mb=100)
                   for row in rows for i in range(count)])
    mem_db.commit()
    data = query(dcs_api[0], list(reversed(rows)), end=START + timedelta(days=7), interval=5)
    assert [s["instance"]["id"] for s in data["series"]] == [r.id for r in reversed(rows)]
    for series in data["series"]:
        assert series["source"] == "LOCAL" and series["status"] == "AVAILABLE"
        assert series["valid_points"] == series["total_points"] == count
        assert series["points"][0]["at"] == START.isoformat().replace("+00:00", "Z")
        assert series["points"][-1]["memory_usage"] == (count - 1) % 75
        assert all(p["memory_max"] == p["memory_usage"] and p["sample_count"] == 1 for p in series["points"])
        assert series["summary"]["peak_usage"] == 74
    assert dcs_api[3] == []


def test_month_history_keeps_actual_samples_without_snapshot_or_legacy_remote_fill(mem_db, dcs_api, monkeypatch):
    row = resource(mem_db, metrics={"memory_usage": 98, "max_memory_mb": 8192})
    sample(mem_db, row, 24 * 60 + 1, 25, used=2048, capacity=8192)
    cloud = install_aom(mem_db, monkeypatch, [RawMemoryPoint(START, 0, 70)])
    cloud.fail_sample_after = 1
    series = query(dcs_api[0], [row], end=START + timedelta(days=31), interval=60)["series"][0]
    assert series["source"] == "LOCAL" and series["status"] == "PARTIAL"
    assert series["valid_points"] == 1 and series["total_points"] == 31 * 24
    assert series["summary"]["average_usage"] == series["summary"]["peak_usage"] == 25
    assert series["points"][0]["memory_usage"] is None
    assert series["points"][24]["memory_usage"] == 25
    assert series["points"][24]["max_memory_mb"] == 8192 and cloud.calls == dcs_api[3] == []


@pytest.mark.parametrize("legacy", ["valid", "unavailable", "wrong_unit", "ambiguous_nodes", "cpu_only", "invalid"])
def test_legacy_aom_metadata_never_starts_dcs_remote_history(mem_db, dcs_api, monkeypatch, legacy):
    row = resource(mem_db, metrics={"memory_usage": 95, "max_memory_mb": 8192})
    cloud = install_aom(mem_db, monkeypatch, [RawMemoryPoint(START, 99, 100)])
    if legacy == "unavailable":
        cloud.sample_status = 404
    elif legacy == "wrong_unit":
        cloud.catalogue_unit = cloud.point_unit = "Count"
    elif legacy == "ambiguous_nodes":
        cloud.catalogue_rows = [{"namespace": AOM_MAPPING["namespace"], "metricName": "memory_used_percent", "unit": "Percent",
                                 "dimensions": [{"name": "cache_id", "value": row.resource_id},
                                                {"name": "node_id", "value": node}]} for node in ("node-a", "node-b")]
    elif legacy == "cpu_only":
        enable_aom(mem_db, mapping={**AOM_MAPPING, "metrics": {"cpu_usage": "cpu_used_percent"}})
    elif legacy == "invalid":
        enable_aom(mem_db, mapping={"invalid": True})
    series = query(dcs_api[0], [row], end=START + timedelta(days=31), interval=60)["series"][0]
    assert series["source"] == "NONE" and series["status"] == "NO_DATA" and series["warning"]
    assert "AOM" not in series["warning"]
    assert all(p["memory_usage"] is None and p["memory_max"] is None for p in series["points"])
    assert cloud.calls == dcs_api[3] == []
    assert mem_db.get(HcsAccount, 1).metrics_source == "AOM"


@pytest.mark.parametrize("mapped_unit", [None, "Percent", "Count", "Byte"])
def test_legacy_units_do_not_override_native_percent_or_mb(mem_db, dcs_api, monkeypatch, mapped_unit):
    row = resource(mem_db)
    sample(mem_db, row, 0, 25, used=256, capacity=1024)
    mapping = {**AOM_MAPPING, **({"units": {"memory_usage": mapped_unit}} if mapped_unit else {})}
    cloud = install_aom(mem_db, monkeypatch, [RawMemoryPoint(START, 0, 7)], mapping=mapping)
    series = query(dcs_api[0], [row], end=START + timedelta(minutes=5), interval=5)["series"][0]
    assert series["source"] == "LOCAL" and series["warning"] is None
    assert series["summary"]["average_usage"] == 25
    assert series["points"][0]["used_memory_mb"] == 256 and series["points"][0]["max_memory_mb"] == 1024
    assert cloud.calls == dcs_api[3] == []
