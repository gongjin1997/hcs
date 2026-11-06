"""数据库迁移：0001 → 0002（租户 / 角色权限 / 审计 / 容器事件）升级、数据迁移（管理员 → 平台管理员角色、tenant_id 回填）、
模型与迁移一致（等价于 alembic check）、可降级。"""
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Text, create_engine, event, inspect, select, text
from sqlalchemy.orm import Session

from app.core.db import Base
from app.models import DcsMemorySample, Resource

BACKEND = Path(__file__).resolve().parents[1]


def _cfg(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.attributes["database_url"] = url
    return cfg


def test_upgrade_migrates_data_and_matches_models(migration_url):
    url = migration_url
    cfg = _cfg(url)
    command.upgrade(cfg, "0001")
    engine = create_engine(url)
    now = "2026-10-01 00:00:00"
    with engine.begin() as c:            # 0001 时代的数据：单账号 + 管理员 / 成员两种身份
        c.execute(text("INSERT INTO hcs_account (id, name, iam_endpoint, domain_name, auth_type, secret_cipher, endpoints,"
                       " enabled, created_at, updated_at) VALUES (1, '生产账号', 'https://iam.example.com', 'prod',"
                       " 'PASSWORD', 'x', '{}', true, :t, :t)"), {"t": now})
        for i, (name, is_admin) in enumerate((("admin", 1), ("zhangwei", 0)), start=1):
            c.execute(text("INSERT INTO sys_user (id, username, display_name, password_hash, is_admin, enabled,"
                           " failed_attempts, created_at, updated_at) VALUES (:i, :n, :n, 'h', :a, true, 0, :t, :t)"),
                      {"i": i, "n": name, "a": bool(is_admin), "t": now})
    command.upgrade(cfg, "head")
    insp = inspect(engine)
    assert {"sys_role", "audit_log", "pod_event", "pod_state", "pod_probe"} <= set(insp.get_table_names())
    assert "is_admin" not in {col["name"] for col in insp.get_columns("sys_user")}
    with engine.connect() as c:
        roles = dict(c.execute(text("SELECT u.username, r.code FROM sys_user u JOIN sys_role r ON r.id = u.role_id")).all())
        assert roles == {"admin": "admin", "zhangwei": "oncall"}
        assert c.execute(text("SELECT code FROM hcs_account")).scalar() == "tenant-1"
        assert c.execute(text("SELECT count(*) FROM sys_role WHERE builtin = true")).scalar() == 4
        diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
    assert diff == []
    command.downgrade(cfg, "0001")
    with engine.connect() as c:
        assert dict(c.execute(text("SELECT username, is_admin FROM sys_user")).all()) == {"admin": 1, "zhangwei": 0}
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine.dispose()


def test_resource_identity_migration_preserves_records_and_blocks_lossy_downgrade(migration_url):
    cfg = _cfg(migration_url)
    command.upgrade(cfg, "0003")
    engine = create_engine(migration_url)
    with Session(engine, expire_on_commit=False) as db:
        original_id = db.execute(Resource.__table__.insert().values(
            service="vpc", resource_type="vpc", resource_id="same", name="保留原记录",
            project_ref=101, tenant_id=1, owner="original-owner")).inserted_primary_key[0]
        db.commit()
    command.upgrade(cfg, "head")
    with Session(engine) as db:
        resource = db.get(Resource, original_id)
        assert resource.name == "保留原记录" and resource.owner == "original-owner"
        db.add(Resource(service="vpc", resource_type="vpc", resource_id="same", name="第二个项目",
                        project_ref=202, tenant_id=2))
        db.commit()
    command.downgrade(cfg, "0004")
    with pytest.raises(RuntimeError, match="跨项目同 ID"):
        command.downgrade(cfg, "0003")
    with Session(engine) as db:
        assert len(db.scalars(select(Resource.id)).all()) == 2
    command.upgrade(cfg, "head")
    engine.dispose()


def test_dcs_memory_history_upgrade_downgrade_and_resource_cascade(migration_url):
    """0006 不从现有资源快照回填；外键、索引与降级均不破坏已有资源。"""
    from datetime import UTC, datetime

    cfg = _cfg(migration_url)
    command.upgrade(cfg, "0005")
    engine = create_engine(migration_url)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
    with Session(engine, expire_on_commit=False) as db:
        original_id = db.execute(Resource.__table__.insert().values(
            service="dcs", resource_type="dcs_instance", resource_id="before-0006", name="原有资源",
            owner="preserved", metrics={"memory_usage": 60})).inserted_primary_key[0]
        db.commit()
    assert "dcs_memory_sample" not in inspect(engine).get_table_names()
    command.upgrade(cfg, "0006")
    inspector = inspect(engine)
    assert {column["name"] for column in inspector.get_columns("dcs_memory_sample")} == {
        "id", "resource_ref", "tenant_id", "project_ref", "observed_at", "memory_usage", "used_memory_mb", "max_memory_mb"}
    indexes = {index["name"]: index for index in inspector.get_indexes("dcs_memory_sample")}
    assert indexes["uk_dcs_memory_resource_time"]["unique"]
    assert indexes["uk_dcs_memory_resource_time"]["column_names"] == ["resource_ref", "observed_at"]
    foreign_key, = inspector.get_foreign_keys("dcs_memory_sample")
    assert foreign_key["referred_table"] == "resource" and foreign_key["options"].get("ondelete") == "CASCADE"
    with Session(engine) as db:
        assert db.scalars(select(DcsMemorySample)).all() == []
        original = db.execute(select(Resource.owner, Resource.metrics).where(Resource.id == original_id)).one()
        assert original.owner == "preserved" and original.metrics["memory_usage"] == 60
        victim_id = db.execute(Resource.__table__.insert().values(
            service="dcs", resource_type="dcs_instance", resource_id="cascade-0006", name="可删除资源")).inserted_primary_key[0]
        db.add(DcsMemorySample(resource_ref=victim_id, observed_at=datetime(2026, 10, 27, tzinfo=UTC), memory_usage=25))
        db.commit()
        db.execute(Resource.__table__.delete().where(Resource.id == victim_id))
        db.commit()
        assert db.scalars(select(DcsMemorySample)).all() == []
    command.downgrade(cfg, "0005")
    assert "dcs_memory_sample" not in inspect(engine).get_table_names()
    command.upgrade(cfg, "0006")
    with Session(engine) as db:
        assert db.scalar(select(Resource.owner).where(Resource.id == original_id)) == "preserved"
        assert db.scalars(select(DcsMemorySample)).all() == []
    engine.dispose()


def test_resource_upstream_text_fields_preserve_long_values_and_guard_downgrade(migration_url):
    cfg = _cfg(migration_url)
    command.upgrade(cfg, "0015")
    engine = create_engine(migration_url)
    with Session(engine) as db:
        item = Resource(service="dcs", resource_type="dcs_instance", resource_id="text-fields",
                        name="long DCS source values", az="zone-a", ip="192.0.2.1", spec="Redis 6.0 | 8 GB")
        db.add(item)
        db.commit()
        resource_id = item.id

    command.upgrade(cfg, "head")
    columns = {column["name"]: column["type"] for column in inspect(engine).get_columns("resource")}
    assert all(isinstance(columns[name], Text) for name in ("az", "ip", "spec"))
    values = {"az": "zone-" + "a" * 80, "ip": ",".join(f"192.0.2.{index}" for index in range(1, 20)),
              "spec": "Redis " + "v" * 140 + " | 8 GB"}
    with Session(engine) as db:
        row = db.get(Resource, resource_id)
        for name, value in values.items():
            setattr(row, name, value)
        db.commit()
    with pytest.raises(RuntimeError, match=r"resource\.az"):
        command.downgrade(cfg, "0015")
    with Session(engine) as db:
        row = db.get(Resource, resource_id)
        assert {name: getattr(row, name) for name in values} == values
        row.az, row.ip, row.spec = "az1", "192.0.2.1", "Redis 6.0 | 8 GB"
        db.commit()
    command.downgrade(cfg, "0015")
    columns = {column["name"]: column["type"] for column in inspect(engine).get_columns("resource")}
    assert columns["az"].length == 64 and columns["ip"].length == columns["spec"].length == 128
    command.upgrade(cfg, "head")
    engine.dispose()
