"""PostgreSQL-only migration and row-lock gates for durable task execution."""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.db import Base
from app.models import HcsAccount, HcsProject, InspectionRun, InspectionTask, SysSetting
from app.models.base import utcnow
from app.models.task_execution import TaskExecution, TaskExecutionCancel
from app.services import execution_runtime, task_execution
from app.services.execution_runtime import ExecutionStopped

from .test_migrations import _cfg

pytestmark = pytest.mark.pg


def test_0023_up_down_up_matches_execution_models(migration_url):
    cfg = _cfg(migration_url)
    command.upgrade(cfg, "0022")
    db_engine = create_engine(migration_url)
    try:
        assert not {"task_execution", "task_execution_cancel"} & set(inspect(db_engine).get_table_names())
        command.upgrade(cfg, "0023")
        inspector = inspect(db_engine)

        for model, table_name in ((TaskExecution, "task_execution"), (TaskExecutionCancel, "task_execution_cancel")):
            columns = {column["name"] for column in inspector.get_columns(table_name)}
            assert columns == set(model.__table__.columns.keys())
        execution_uniques = {tuple(item["column_names"]) for item in inspector.get_unique_constraints("task_execution")}
        assert {("run_id",), ("active_scope",), ("request_key", "scope_key")} <= execution_uniques
        indexes = {item["name"] for item in inspector.get_indexes("task_execution")}
        assert {"ix_task_execution_task_code", "ix_task_execution_request_id", "ix_task_execution_queue",
                "ix_task_execution_scope_time"} <= indexes
        run_fk = next(fk for fk in inspector.get_foreign_keys("task_execution") if fk["constrained_columns"] == ["run_id"])
        cancel_fk = inspector.get_foreign_keys("task_execution_cancel")[0]
        assert run_fk["referred_table"] == "inspection_run" and run_fk["options"].get("ondelete") == "CASCADE"
        assert cancel_fk["referred_table"] == "task_execution" and cancel_fk["options"].get("ondelete") == "CASCADE"

        with db_engine.connect() as connection:
            diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert diff == []

        with Session(db_engine, expire_on_commit=False) as db:
            run = InspectionRun(task_code="dcs_status", tenant_id=1, project_ref=101, status="QUEUED",
                                started_at=utcnow())
            db.add(run)
            db.flush()
            execution = TaskExecution(id=str(uuid4()), run_id=run.id, task_code=run.task_code, tenant_id=1,
                project_ref=101, request_id=uuid4().hex, request_key=uuid4().hex, scope_key=uuid4().hex,
                active_scope=uuid4().hex, trigger_type="MANUAL", requested_by="pg-migration-test",
                status="QUEUED", queue_expires_at=utcnow() + timedelta(minutes=5), timeout_sec=300,
                params={}, target={}, progress={}, history=[])
            db.add(execution)
            db.commit()
            with pytest.raises(RuntimeError, match="仍有排队或运行中的巡检"):
                command.downgrade(cfg, "0022")
            assert "task_execution" in inspect(db_engine).get_table_names()
            execution.status, execution.active_scope = "SUCCESS", None
            execution.finished_at = utcnow()
            run.status, run.finished_at = "SUCCESS", execution.finished_at
            db.commit()

        command.downgrade(cfg, "0022")
        assert not {"task_execution", "task_execution_cancel"} & set(inspect(db_engine).get_table_names())
        command.upgrade(cfg, "head")
        with db_engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    finally:
        db_engine.dispose()


@pytest.fixture()
def execution_db(mem_db):
    assert mem_db.get_bind().dialect.name == "postgresql"
    mem_db.add_all([
        InspectionTask(code="dcs_status", name="DCS", service="dcs", interval_sec=120, enabled=True, params={}),
        HcsAccount(id=1, code="pg-tenant", name="PG tenant", short_name="PG", iam_endpoint="https://example.invalid",
                   domain_name="pg-test", secret_cipher=""),
    ])
    mem_db.flush()
    mem_db.add(HcsProject(id=101, account_id=1, region_id="pg-region", project_id="pg-project",
                          project_name="PG project", external_domain="example.invalid"))
    mem_db.commit()
    return mem_db


def _sessions(db):
    return sessionmaker(bind=db.get_bind(), expire_on_commit=False, autoflush=False)


def _enqueue(db, *, request_key: str | None = None):
    return task_execution.enqueue(db, "dcs_status", tenant_ids=[1], project_refs=[101],
                                  request_key=request_key or uuid4().hex)


def test_concurrent_enqueue_same_scope_creates_one_durable_execution(execution_db):
    session_factory = _sessions(execution_db)
    workers = 8
    barrier = Barrier(workers)

    def submit(index: int):
        with session_factory() as db:
            barrier.wait(timeout=10)
            return _enqueue(db, request_key=f"parallel-{index}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = [future.result(timeout=20) for future in [pool.submit(submit, i) for i in range(workers)]]

    execution_ids = {outcome["execution_ids"][0] for outcome in outcomes}
    assert len(execution_ids) == 1
    assert all(len(outcome["execution_ids"]) == 1 for outcome in outcomes)
    assert sum(not outcome["deduplicated"] for outcome in outcomes) == 1
    assert execution_db.query(TaskExecution).count() == execution_db.query(InspectionRun).count() == 1


def test_concurrent_claim_next_has_exactly_one_owner_for_one_queue_row(execution_db):
    queued = _enqueue(execution_db)["execution_ids"][0]
    session_factory = _sessions(execution_db)
    workers = 8
    barrier = Barrier(workers)

    def claim(index: int):
        with session_factory() as db:
            barrier.wait(timeout=10)
            row = task_execution.claim_next(db, f"pg-worker-{index}")
            return None if row is None else (row.id, row.owner_token, row.worker_id)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        claims = [future.result(timeout=20) for future in [pool.submit(claim, i) for i in range(workers)]]

    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1 and winners[0][0] == queued
    assert winners[0][1] and winners[0][2].startswith("pg-worker-")
    execution_db.expire_all()
    row = execution_db.get(TaskExecution, queued)
    run = execution_db.get(InspectionRun, row.run_id)
    assert row.status == run.status == "RUNNING" and row.owner_token == winners[0][1]


def test_guard_no_key_update_lock_allows_cancel_fk_then_rejects_worker_commit(execution_db):
    execution_id = _enqueue(execution_db)["execution_ids"][0]
    claimed = task_execution.claim_next(execution_db, "pg-guard-worker")
    assert claimed is not None and claimed.id == execution_id and claimed.owner_token is not None
    worker_db = _sessions(execution_db)()
    cancel_committed = Event()
    lock_held = Event()

    try:
        # This is the same PostgreSQL row-lock mode used by execution_runtime.managed.
        worker_db.scalar(select(TaskExecution).where(TaskExecution.id == execution_id)
                         .execution_options(populate_existing=True).with_for_update(key_share=True))
        lock_held.set()

        def cancel():
            assert lock_held.wait(timeout=5)
            with _sessions(execution_db)() as cancel_db:
                task_execution.request_cancel(cancel_db, execution_id, "pg-cancel-operator")
            cancel_committed.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(cancel)
            # The FK's KEY SHARE lock must remain compatible with the guard's NO KEY UPDATE lock.
            assert cancel_committed.wait(timeout=5), "cancel commit blocked behind the worker guard row lock"
            future.result(timeout=5)

        control = execution_runtime.Control(
            claimed.id, claimed.run_id, claimed.owner_token, time.monotonic() + 30,
            dict(claimed.params or {}), lambda _progress: None)
        with execution_runtime.managed(worker_db, control):
            worker_db.add(SysSetting(key="pg-cancelled-result", value={"saved": True}))
            with pytest.raises(ExecutionStopped, match="已取消"):
                worker_db.commit()
            worker_db.rollback()
    finally:
        worker_db.close()

    execution_db.expire_all()
    cancelled = execution_db.get(TaskExecutionCancel, execution_id)
    final = execution_db.get(TaskExecution, execution_id)
    assert cancelled is not None and final is not None and final.status == "RUNNING"
    assert execution_db.get(SysSetting, "pg-cancelled-result") is None
