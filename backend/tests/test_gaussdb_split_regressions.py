"""Gauss 拆分回归：规格归属、非法状态，以及真实 PG 首次并发写入；仅用合成云和隔离测试库。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from threading import Barrier, Lock

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from app.collectors import gaussdb
from app.collectors.base import RunContext
from app.collectors.gaussdb import GaussDBCollector, GaussDBComponentsCollector, GaussDBStorageCollector
from app.core.config import get_settings
from app.models import InspectionTask, Resource
from app.services import pipeline

AT = datetime(2026, 11, 9, 3, tzinfo=UTC)
KIND = "gaussdb_instance"
LIST = "/gaussdb/v3.3/{project_id}/instances"
MISSING = object()
COLLECTORS = (GaussDBCollector, GaussDBStorageCollector, GaussDBComponentsCollector)


def instance(rid, status="ACTIVE"):
    row = {"id": rid, "name": rid, "nodes": []}
    if status is not MISSING:
        row["status"] = status
    return row


class SyntheticCloud:
    max_concurrency = 2

    def __init__(self, instances):
        self.instances = deepcopy(instances)
        self.calls = []

    def get(self, service, path, *, params=None):
        assert service == "gaussdb"
        self.calls.append((path, deepcopy(params)))
        if path == LIST:
            assert params == {"offset": 0, "limit": 100}
            return {"total_count": len(self.instances), "instances": deepcopy(self.instances)}
        rid = path.split("/")[-2]
        assert rid in {row["id"] for row in self.instances}
        prefix = f"/gaussdb/v3/{{project_id}}/instances/{rid}"
        if path == f"{prefix}/volume-usage":
            assert params is None
            return {"used": "25", "total": "100"}
        assert path == f"{prefix}/components" and params == {"offset": 0, "limit": 100}
        return {"total_count": 1, "nodes": [{"id": f"node-{rid}", "name": f"node-{rid}", "components": [
            {"id": f"dn-{rid}", "type": "DN", "status": "Primary"}]}]}


@pytest.fixture(autouse=True)
def native_settings(monkeypatch):
    monkeypatch.setattr(get_settings(), "hcs_gaussdb_api_variant", "v3.3")
    monkeypatch.setattr(gaussdb, "utcnow", lambda: AT)


def context(collector, cloud, db=None):
    return RunContext(client=cloud, account="split-regression", project_id="synthetic-project", tenant_id=101,
                      project_ref=1001, db=db, params={"_task_code": collector.code, "_inspection_stale_after_sec": 900})


def configure(db):
    db.add_all(InspectionTask(code=collector.code, name=collector.name, service="gaussdb", interval_sec=120,
                              enabled=True, params={}) for collector in COLLECTORS)
    db.commit()


@pytest.mark.parametrize("collector", [GaussDBStorageCollector, GaussDBComponentsCollector])
@pytest.mark.parametrize("memory_verified", [False, True])
def test_detail_tasks_preserve_old_spec_without_current_unit_flags(mem_db, collector, memory_verified):
    configure(mem_db)
    old_attrs = {"memory_unit_verified": memory_verified, "volume_unit_verified": False, "memory_size": 32,
                 "volume_size": 500, "type": "distributed", "vcpus": 8,
                 "gaussdb_monitoring": {"status": "AVAILABLE", "observed_at": "2026-11-09T02:00:00+00:00"},
                 "gaussdb_components": {"state": "AVAILABLE", "total": 2, "items": [{"component_id": "old-dn"}]}}
    old_metrics = {"cpu_usage": 99, "mem_usage": 98, "disk_usage": 50, "disk_used_gb": 100, "disk_total_gb": 200}
    resource = Resource(service="gaussdb", resource_type=KIND, resource_id="one", name="one", tenant_id=101,
                        project_ref=1001, memory_gb=64, storage_gb=500, spec="64 GB | 500 GB", status_raw="SHUTDOWN",
                        attrs=deepcopy(old_attrs), metrics=deepcopy(old_metrics))
    mem_db.add(resource)
    mem_db.commit()
    resource_ref = resource.id
    cloud = SyntheticCloud([instance("one")])
    ctx = context(collector, cloud, mem_db)
    rows = collector().collect(ctx)
    assert len(rows) == 1 and rows[0].status_raw is None and ctx.errors == []
    assert not {"memory_unit_verified", "volume_unit_verified"}.intersection(rows[0].attrs)

    pipeline.process_observations(mem_db, "gaussdb", rows, ctx, AT)

    mem_db.expire_all()
    resource = mem_db.get(Resource, resource_ref)
    assert (resource.memory_gb, resource.storage_gb, resource.spec) == (64, 500, "64 GB | 500 GB")
    assert resource.status_raw == "SHUTDOWN"
    assert all(resource.attrs[key] == old_attrs[key] for key in (
        "memory_unit_verified", "volume_unit_verified", "memory_size", "volume_size", "type", "vcpus"))
    assert resource.metrics["cpu_usage"] == 99 and resource.metrics["mem_usage"] == 98
    if collector is GaussDBStorageCollector:
        assert resource.metrics == {"cpu_usage": 99, "mem_usage": 98,
                                    "disk_usage": 25, "disk_used_gb": 25, "disk_total_gb": 100}
        assert resource.attrs["gaussdb_monitoring"]["observed_at"] == AT.isoformat()
        assert resource.attrs["gaussdb_components"] == old_attrs["gaussdb_components"]
    else:
        assert resource.metrics == old_metrics
        assert resource.attrs["gaussdb_components"]["observed_at"] == AT.isoformat()
        assert resource.attrs["gaussdb_components"]["total"] == 1
        assert resource.attrs["gaussdb_monitoring"] == old_attrs["gaussdb_monitoring"]


@pytest.mark.parametrize("collector", COLLECTORS)
@pytest.mark.parametrize("status", [
    pytest.param(MISSING, id="missing"), pytest.param(None, id="null"), pytest.param("", id="empty"),
    pytest.param("FUTURE_STATE", id="unknown-enum"), pytest.param(123, id="number"), pytest.param(False, id="boolean"),
    pytest.param([], id="empty-list"), pytest.param({}, id="empty-object"),
    pytest.param(["ACTIVE"], id="list"), pytest.param({"status": "ACTIVE"}, id="object"),
])
def test_unknown_instance_status_does_not_discard_other_instances(collector, status):
    cloud = SyntheticCloud([instance("invalid", status), instance("healthy")])
    ctx = context(collector, cloud)

    rows = collector().collect(ctx)

    invalid, healthy = rows
    assert [row.resource_id for row in rows] == ["invalid", "healthy"]
    assert ctx.stats["gaussdb"]["instances"] == 2 and ctx.stats["gaussdb"]["online"] == 1
    assert ctx.stats["gaussdb"]["failed"] == 0
    if collector is GaussDBCollector:
        assert invalid.facts == {"status": None} and healthy.facts == {"status": "ACTIVE"}
        if not isinstance(status, str):
            assert invalid.status_raw is None
        assert len(ctx.errors) == 1 and "缺失或未识别" in ctx.errors[0]
        assert cloud.calls == [(LIST, {"offset": 0, "limit": 100})]
    else:
        suffix = "volume-usage" if collector is GaussDBStorageCollector else "components"
        params = None if collector is GaussDBStorageCollector else {"offset": 0, "limit": 100}
        assert cloud.calls == [(LIST, {"offset": 0, "limit": 100}),
                               (f"/gaussdb/v3/{{project_id}}/instances/healthy/{suffix}", params)]
        assert invalid.status_raw is None
        if collector is GaussDBStorageCollector:
            assert invalid.facts == {"disk_usage": None} and invalid.storage_status == "UNAVAILABLE"
            assert healthy.facts == {"disk_usage": 25} and healthy.storage_status == "AVAILABLE"
        else:
            assert invalid.facts == {"down_components": None, "unknown_components": None, "down_list": ""}
            assert healthy.facts == {"down_components": 0, "unknown_components": 0, "down_list": ""}


@pytest.mark.pg
@pytest.mark.parametrize("resource_ids", [("first",), ("a", "b")], ids=["same-instance", "opposite-instance-order"])
def test_pg_simultaneous_first_writes_keep_one_resource_and_both_task_metadata(mem_db, resource_ids):
    configure(mem_db)
    engine = mem_db.get_bind()
    assert engine.dialect.name == "postgresql"
    assert mem_db.scalar(select(func.count()).select_from(Resource)) == 0
    mem_db.rollback()
    barrier, guard = Barrier(2), Lock()
    arrived = set()
    worker_key = "gauss_split_regression_worker"

    def before_insert(conn, cursor, statement, parameters, execution_context, executemany):
        compiled = execution_context.compiled
        if compiled is None or not getattr(compiled.statement, "is_insert", False):
            return
        if compiled.statement.table.name != "resource":
            return
        worker = conn.info.get(worker_key)
        with guard:
            if worker is None or worker in arrived:
                return
            arrived.add(worker)
        # 必须在真实 INSERT 发出前让双方均到达，不能靠线程启动时机碰运气。
        barrier.wait(timeout=8)

    def worker(collector, ids):
        with Session(engine, autoflush=False, expire_on_commit=False) as db:
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            db.execute(text("SET LOCAL statement_timeout = '12s'"))
            backend_pid = db.scalar(select(func.pg_backend_pid()))
            info = db.connection().info
            info[worker_key] = collector.code
            try:
                cloud = SyntheticCloud([instance(rid) for rid in ids])
                ctx = context(collector, cloud, db)
                rows = collector().collect(ctx)
                assert ctx.errors == []
                pipeline.process_observations(db, "gaussdb", rows, ctx, AT)
                return backend_pid
            finally:
                info.pop(worker_key, None)

    event.listen(engine, "before_cursor_execute", before_insert)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, GaussDBStorageCollector, resource_ids),
                       pool.submit(worker, GaussDBComponentsCollector, tuple(reversed(resource_ids)))]
            try:
                backend_pids = [future.result(timeout=20) for future in futures]
            finally:
                barrier.abort()
    finally:
        event.remove(engine, "before_cursor_execute", before_insert)

    assert len(set(backend_pids)) == 2  # 两条真实 PG 连接，各自拥有独立事务。
    assert arrived == {GaussDBStorageCollector.code, GaussDBComponentsCollector.code}
    mem_db.expire_all()
    rows = list(mem_db.scalars(select(Resource).order_by(Resource.resource_id)))
    assert [row.resource_id for row in rows] == sorted(resource_ids)
    for row in rows:
        assert (row.tenant_id, row.project_ref, row.resource_type) == (101, 1001, KIND)
        assert row.metrics == {"disk_usage": 25, "disk_used_gb": 25, "disk_total_gb": 100}
        assert row.attrs["gaussdb_monitoring"]["status"] == "AVAILABLE"
        assert row.attrs["gaussdb_monitoring"]["observed_at"] == AT.isoformat()
        summary = row.attrs["gaussdb_components"]
        assert summary["state"] == "AVAILABLE" and summary["observed_at"] == AT.isoformat()
        assert summary["total"] == 1 and summary["items"][0]["component_id"] == f"dn-{row.resource_id}"
        tasks = row.attrs["inspection_monitoring"]["tasks"]
        assert set(tasks) == {GaussDBStorageCollector.code, GaussDBComponentsCollector.code}
        assert all(value["evaluated_at"] == AT.isoformat() for value in tasks.values())
