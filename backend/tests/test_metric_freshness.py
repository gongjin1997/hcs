"""切换指标源后，本轮缺失的使用率不能继续展示旧 CES 数值。"""
from types import SimpleNamespace

import pytest

from app.collectors.base import RunContext
from app.collectors.ecs import EcsStatusCollector
from app.collectors.gaussdb import GaussDBCollector


@pytest.mark.parametrize("collector,body,fields", [
    (EcsStatusCollector(), {"servers": [{"id": "one", "name": "one", "status": "ACTIVE"}], "count": 1},
     ("cpu_util", "mem_util", "disk_util", "disk_mount")),
    (GaussDBCollector(), {"instances": [{"id": "one", "name": "one", "status": "ACTIVE"}], "total_count": 1},
     ("cpu_usage", "mem_usage", "disk_usage")),
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
    merged = {field: 99.9 for field in fields}
    for row in rows:
        merged.update(row.metrics)
    assert all(merged[field] is None for field in fields)
    assert status.facts["status"] == "ACTIVE"
    assert status.attrs["metrics_source"] == ("GAUSSDB" if collector.service == "gaussdb" else "NONE")
    # 合成磁盘响应不完整时保持缺失；不回退 AOM/CES 或沿用旧值。GaussDB 另查一次组件（与磁盘互不影响）。
    assert len(calls) == (3 if collector.service == "gaussdb" else 1)
