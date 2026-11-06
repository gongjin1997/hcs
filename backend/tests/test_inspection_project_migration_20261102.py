"""0015 standalone SQLite round-trip and offline PostgreSQL DDL, without any server."""
from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations


def migration():
    path = Path(__file__).parents[1] / "migrations/versions/20261102_0015_inspection_run_project_scope.py"
    spec = spec_from_file_location("inspection_project_0015", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0015" and module.down_revision == "0014"
    return module


def test_sqlite_0015_preserves_legacy_rows_and_does_not_infer_project():
    engine = sa.create_engine("sqlite://")
    module = migration()
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE inspection_run (id INTEGER PRIMARY KEY, task_code VARCHAR(64) NOT NULL, "
                             "tenant_id INTEGER, started_at DATETIME NOT NULL, status VARCHAR(16) NOT NULL)")
        conn.exec_driver_sql("INSERT INTO inspection_run VALUES (1, 'job', 123, '2026-11-02 00:00:00', 'FAILED')")
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            assert conn.exec_driver_sql("SELECT project_ref FROM inspection_run").scalar() is None
            columns = {col["name"]: col for col in sa.inspect(conn).get_columns("inspection_run")}
            assert columns["project_ref"]["nullable"]
            indexes = {index["name"] for index in sa.inspect(conn).get_indexes("inspection_run")}
            assert "idx_inspection_run_scope_time" in indexes
            conn.exec_driver_sql("INSERT INTO inspection_run VALUES (2, 'job', 123, '2026-11-02 01:00:00', 'SUCCESS', 456)")
            module.downgrade()
            assert "project_ref" not in {col["name"] for col in sa.inspect(conn).get_columns("inspection_run")}
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM inspection_run").scalar() == 2
            module.upgrade()
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM inspection_run WHERE project_ref IS NULL").scalar() == 2
    engine.dispose()


def test_postgresql_0015_emits_reversible_native_ddl_without_connecting():
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    module = migration()
    with Operations.context(context):
        module.upgrade()
        module.downgrade()
    sql = output.getvalue()
    assert "ADD COLUMN project_ref INTEGER" in sql
    assert "CREATE INDEX idx_inspection_run_scope_time" in sql
    assert "DROP INDEX idx_inspection_run_scope_time" in sql
    assert "DROP COLUMN project_ref" in sql
    assert "UPDATE inspection_run" not in sql


def test_actual_database_0015_roundtrip_preserves_rows_without_inference(migration_url):
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.attributes["database_url"] = migration_url
    command.upgrade(config, "0014")
    engine = sa.create_engine(migration_url)
    try:
        before = sa.Table("inspection_run", sa.MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(before.insert().values(task_code="ecs_status", tenant_id=99, trigger_type="MANUAL",
                started_at=datetime(2026, 11, 2, tzinfo=UTC), status="FAILED", resource_total=4,
                abnormal_total=1, api_calls=2, stats={"synthetic": True}))
        command.upgrade(config, "0015")
        current = sa.Table("inspection_run", sa.MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            row = connection.execute(sa.select(current)).mappings().one()
            assert row["project_ref"] is None and row["resource_total"] == 4
            assert row["stats"] == {"synthetic": True}
            connection.execute(current.update().values(project_ref=123))
        command.downgrade(config, "0014")
        assert "project_ref" not in {column["name"] for column in sa.inspect(engine).get_columns("inspection_run")}
        command.upgrade(config, "0015")
        with engine.connect() as connection:
            row = connection.execute(sa.select(current)).mappings().one()
            assert row["tenant_id"] == 99 and row["project_ref"] is None
            assert row["status"] == "FAILED" and row["stats"] == {"synthetic": True}
    finally:
        engine.dispose()
