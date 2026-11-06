"""0012→0013→0012→0013：独立迁移库、不回填配置/历史、不触碰现有资源。"""
from datetime import UTC, datetime

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select
from sqlalchemy.orm import Session

from app.models import GaussDBStorageSample


def test_gaussdb_0013_upgrade_downgrade_upgrade_preserves_inventory(migration_url):
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = migration_url
    command.upgrade(cfg, "0012")
    engine = create_engine(migration_url)
    try:
        resources = Table("resource", MetaData(), autoload_with=engine)
        now = datetime(2026, 11, 2, tzinfo=UTC)
        with engine.begin() as connection:
            rid = connection.execute(resources.insert().values(service="gaussdb", resource_type="gaussdb_instance",
                resource_id="before-gauss-0013", name="保留资源", owner="keep-owner", metrics={"disk_usage": 90},
                attrs={"volume_size": 999}, health="UNKNOWN", labels={}, expected_state={}, monitored=True,
                critical=False, lifecycle="ACTIVE", first_seen=now, last_seen=now)).inserted_primary_key[0]
            original = dict(connection.execute(select(resources).where(resources.c.id == rid)).mappings().one())
        assert "gaussdb_storage_sample" not in inspect(engine).get_table_names()
        command.upgrade(cfg, "0013")
        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("gaussdb_storage_sample")}
        assert set(columns) == {
            "id", "resource_ref", "tenant_id", "project_ref", "observed_at", "collected_at", "status", "used_gb",
            "total_gb", "usage_percent"}
        assert not columns["observed_at"]["nullable"] and not columns["collected_at"]["nullable"]
        indexes = {i["name"]: i for i in inspector.get_indexes("gaussdb_storage_sample")}
        assert indexes["uk_gaussdb_storage_resource_time"]["unique"]
        assert indexes["idx_gaussdb_storage_scope_time"]["column_names"] == [
            "tenant_id", "project_ref", "resource_ref", "observed_at"]
        fk, = inspector.get_foreign_keys("gaussdb_storage_sample")
        assert fk["referred_table"] == "resource" and fk["options"]["ondelete"] == "CASCADE"
        resources = Table("resource", MetaData(), autoload_with=engine)
        with Session(engine) as db:
            assert not db.scalars(select(GaussDBStorageSample)).all()
            assert dict(db.execute(select(resources).where(resources.c.id == rid)).mappings().one()) == original
            received_at = datetime(2026, 11, 2, 1, 2, 3, 123000, tzinfo=UTC)
            db.add(GaussDBStorageSample(resource_ref=rid, tenant_id=1, project_ref=11,
                observed_at=received_at, collected_at=received_at, status="AVAILABLE", used_gb=25, total_gb=100,
                usage_percent=25))
            db.commit()
            stored = db.scalar(select(GaussDBStorageSample))
            assert stored.observed_at == stored.collected_at == received_at
        command.downgrade(cfg, "0012")
        assert "gaussdb_storage_sample" not in inspect(engine).get_table_names()
        command.upgrade(cfg, "0013")
        resources = Table("resource", MetaData(), autoload_with=engine)
        with Session(engine) as db:
            assert not db.scalars(select(GaussDBStorageSample)).all()
            assert dict(db.execute(select(resources).where(resources.c.id == rid)).mappings().one()) == original
    finally:
        engine.dispose()
