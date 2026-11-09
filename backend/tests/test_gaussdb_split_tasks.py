"""独立任务、受限并发和可诊断耗时；全部使用合成云响应。"""
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from app.collectors.base import RunContext
from app.collectors.gaussdb import GaussDBCollector, GaussDBComponentsCollector, GaussDBStorageCollector
from app.core.logging import _context, log_context
from app.hcs import HcsError
from app.hcs.diagnostics import logical_path
from app.models import InspectionTask
from app.services import bootstrap
from app.services.catalog import TASK_BY_CODE, TASK_STEPS


class Cloud:
    def __init__(self, *, count=5, delay=0.025, fail=None, state="ACTIVE", concurrency=2):
        self.count, self.delay, self.fail, self.state = count, delay, fail, state
        self.max_concurrency = concurrency
        self.calls, self.contexts = [], []
        self.active = self.peak = 0
        self.lock = threading.Lock()

    def get(self, service, path, **_kwargs):
        assert service == "gaussdb"
        with self.lock:
            self.calls.append(path)
        if path.endswith("/instances"):
            return {"total_count": self.count, "instances": [
                {"id": f"g-{n}", "name": f"db-{n}", "status": self.state, "nodes": []} for n in range(self.count)]}
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.contexts.append(dict(_context.get() or {}))
        try:
            time.sleep(self.delay)
            if path.split("/")[-2] == self.fail:
                raise HcsError(0, "NETWORK", "synthetic timeout")
            if path.endswith("/volume-usage"):
                return {"used": "25", "total": "100"}
            assert path.endswith("/components")
            return {"total_count": 1, "nodes": [{"id": "node-1", "name": "dn-1", "components": [
                {"id": "dn_1", "type": "DN", "status": "Primary"}]}]}
        finally:
            with self.lock:
                self.active -= 1


def context(cloud, concurrency=16):
    return RunContext(client=cloud, account="synthetic", project_id="p", params={"hcs_concurrency": concurrency})


def test_status_is_one_list_call_and_leaves_disk_and_component_samples_untouched():
    cloud = Cloud()
    ctx = context(cloud)
    observations = GaussDBCollector().collect(ctx)
    assert len(cloud.calls) == 1
    assert len(observations) == 5 and {o.check_item for o in observations} == {"instance_status"}
    assert all("disk_usage" not in o.metrics and "gaussdb_monitoring" not in o.attrs
               and "gaussdb_components" not in o.attrs for o in observations)
    assert ctx.stats["gaussdb"]["queried"] == 0
    assert ctx.stats["gaussdb"]["detail_ms"] == 0


@pytest.mark.parametrize("collector,suffix", [(GaussDBStorageCollector, "volume-usage"),
                                              (GaussDBComponentsCollector, "components")])
def test_five_instances_use_bounded_parallel_detail_requests(collector, suffix):
    cloud = Cloud(delay=0.05)
    ctx = context(cloud)
    with log_context(task=collector.code, run_id=42):
        observations = collector().collect(ctx)
    assert [o.resource_id for o in observations] == [f"g-{n}" for n in range(5)]
    assert len(cloud.calls) == 6 and all(path.endswith(suffix) for path in cloud.calls[1:])
    assert cloud.peak == 2 and cloud.active == 0
    assert all(c["task"] == collector.code and c["run_id"] == 42 and c["resource_id"].startswith("g-")
               for c in cloud.contexts)
    stats = ctx.stats["gaussdb"]
    assert stats["instances"] == stats["online"] == stats["queried"] == 5
    assert stats["failed"] == 0 and stats["concurrency"] == 2
    # 与本轮实际累积明细等待比较，不依赖机器速度的绝对秒数。
    assert stats["detail_ms"] < stats["detail_total_ms"] * 0.85
    assert stats["wall_ms"] >= stats["detail_ms"] and stats["detail_max_ms"] > 0


@pytest.mark.parametrize("collector", [GaussDBStorageCollector, GaussDBComponentsCollector])
def test_single_concurrency_and_one_failed_instance_do_not_discard_other_results(collector, caplog):
    cloud = Cloud(fail="g-2", delay=0)
    ctx = context(cloud, concurrency=1)
    with caplog.at_level(logging.INFO):
        observations = collector().collect(ctx)
    assert cloud.peak == 1 and len(observations) == 5
    assert len(ctx.errors) == 1 and "NETWORK" in ctx.errors[0]
    assert ctx.stats["gaussdb"]["failed"] == 1
    failed = observations[2]
    if collector is GaussDBStorageCollector:
        assert failed.storage_status == "ERROR" and failed.metrics["disk_usage"] is None
        assert observations[0].metrics["disk_usage"] == 25
    else:
        assert failed.facts["down_components"] is None
        assert observations[0].facts["down_components"] == 0
    records = [r for r in caplog.records if r.msg == "gaussdb_detail_finished"]
    assert len(records) == 5 and sum(r.result == "ERROR" for r in records) == 1


@pytest.mark.parametrize("collector", [GaussDBStorageCollector, GaussDBComponentsCollector])
def test_non_online_instances_do_not_issue_detail_requests(collector):
    cloud = Cloud(state="REBOOTING")
    ctx = context(cloud)
    rows = collector().collect(ctx)
    assert len(cloud.calls) == 1 and len(rows) == 5 and cloud.peak == 0
    assert ctx.stats["gaussdb"]["online"] == ctx.stats["gaussdb"]["queried"] == 0


@pytest.mark.parametrize("enabled", [False, True])
def test_upgrade_splits_existing_schedule_without_enabling_previously_disabled_work(mem_db, monkeypatch, enabled):
    monkeypatch.setattr(bootstrap, "get_settings", lambda: SimpleNamespace(env="prod"))
    parent = InspectionTask(code="gaussdb_status", name="GaussDB 实例状态与容量", service="gaussdb",
                            interval_sec=720, cron="15 * * * *", enabled=enabled, params={"preserve": 1})
    mem_db.add(parent)
    mem_db.flush()
    bootstrap.ensure_tasks(mem_db)
    assert parent.name == "GaussDB 实例状态" and parent.params == {"preserve": 1}
    for code in ("gaussdb_storage", "gaussdb_components"):
        task = mem_db.get(InspectionTask, code)
        assert (task.enabled, task.interval_sec, task.cron) == (enabled, 720, "15 * * * *")
    child = mem_db.get(InspectionTask, "gaussdb_storage")
    child.enabled, child.interval_sec, child.cron = False, 1800, None
    bootstrap.ensure_tasks(mem_db)
    assert (child.enabled, child.interval_sec, child.cron) == (False, 1800, None)


def test_split_catalog_has_exact_check_ownership_and_execution_steps():
    expected = {"gaussdb_status": ("instance_status",), "gaussdb_storage": ("disk",),
                "gaussdb_components": ("components",)}
    for code, checks in expected.items():
        assert TASK_BY_CODE[code].checks == checks
        assert TASK_BY_CODE[code].types == ("gaussdb_instance",) and TASK_STEPS[code]
    assert set(TASK_STEPS) == set(TASK_BY_CODE)


def test_diagnostic_path_keeps_operation_but_never_resource_identity():
    for operation in ("volume-usage", "components"):
        assert logical_path(f"/gaussdb/v3/{{project_id}}/instances/private-instance/{operation}") == (
            f"/gaussdb/v3/{{project_id}}/instances/{{item}}/{operation}")


def test_task_api_exposes_independent_checks_and_steps(client):
    from .conftest import login

    headers = login(client, "admin", "admin123")
    response = client.get("/api/v1/system/tasks", headers=headers)
    assert response.status_code == 200
    tasks = {row["code"]: row for row in response.json()["data"]}
    for code, check in (("gaussdb_status", "instance_status"), ("gaussdb_storage", "disk"),
                        ("gaussdb_components", "components")):
        assert tasks[code]["checks"] == [check]
        assert tasks[code]["steps"] == list(TASK_STEPS[code])
