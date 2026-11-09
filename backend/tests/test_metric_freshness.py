"""切换指标源后，本轮缺失的使用率不能继续展示旧 CES 数值。"""
from types import SimpleNamespace

import pytest

from app.collectors.base import RunContext
from app.collectors.ecs import EcsStatusCollector
from app.collectors.gaussdb import GaussDBCollector, GaussDBStorageCollector


@pytest.mark.parametrize("collector,body,fields", [
    (EcsStatusCollector(), {"servers": [{"id": "one", "name": "one", "status": "ACTIVE"}], "count": 1},
     ("cpu_util", "mem_util", "disk_util", "disk_mount")),
    (GaussDBCollector(), {"instances": [{"id": "one", "name": "one", "status": "ACTIVE"}], "total_count": 1},
     ("cpu_usage", "mem_usage")),
])
def test_absent_metrics_clear_previous_source_values_but_keep_native_status(collector, body, fields):
    calls = []
    def get(service, path, **_kwargs):
        calls.append((service, path))
        assert service == collector.service
        return body
    client = SimpleNamespace(get=get, mock_url=None, resolver=SimpleNamespace(templates={}))
    tenant = SimpleNamespace(metrics_source="NONE", endpoints={}, capabilities={})
    rows = collector.collect(RunContext(client=client, account="test", project_id="project", tenant=tenant))
    status = next(row for row in rows if row.check_item == "instance_status")
    # 与真实流水线相同的增量合并结果：旧值必须被当前缺失值覆盖。
    merged = {"disk_usage": 97, "disk_used_gb": 194, "disk_total_gb": 200, **{field: 99.9 for field in fields}}
    for row in rows:
        merged.update(row.metrics)
    assert all(merged[field] is None for field in fields)
    assert status.facts["status"] == "ACTIVE"
    assert status.attrs["metrics_source"] == ("GAUSSDB" if collector.service == "gaussdb" else "NONE")
    assert len(calls) == 1
    if collector.service == "gaussdb":
        assert merged == {"cpu_usage": None, "mem_usage": None, "disk_usage": 97,
                          "disk_used_gb": 194, "disk_total_gb": 200}
        assert status.metrics == {"cpu_usage": None, "mem_usage": None}


def test_absent_gaussdb_storage_metrics_clear_previous_values_without_changing_status():
    calls = []

    def get(service, path, **_kwargs):
        calls.append((service, path))
        assert service == "gaussdb"
        if path.endswith("/instances"):
            return {"instances": [{"id": "one", "name": "one", "status": "ACTIVE"}], "total_count": 1}
        assert path == "/gaussdb/v3/{project_id}/instances/one/volume-usage"
        return {}  # 响应缺失用量，不能回退 AOM/CES 或继续沿用旧值。

    ctx = RunContext(client=SimpleNamespace(get=get), account="test", project_id="project")
    row, = GaussDBStorageCollector().collect(ctx)
    assert row.check_item == "disk" and row.status_raw is None
    assert row.facts == {"disk_usage": None} and row.storage_status == "ERROR"
    merged = {"cpu_usage": 10, "mem_usage": 20, "disk_usage": 99.9, "disk_used_gb": 999, "disk_total_gb": 1000}
    merged.update(row.metrics)
    assert merged == {"cpu_usage": 10, "mem_usage": 20, "disk_usage": None, "disk_used_gb": None, "disk_total_gb": None}
    assert row.attrs["gaussdb_monitoring"]["status"] == "ERROR" and row.attrs["metrics_source"] == "GAUSSDB"
    assert ctx.errors == ["GaussDB 原生磁盘用量查询失败（LOCAL.GAUSSDB_RESPONSE）"]
    assert len(calls) == 2
