"""0021：旧租户/旧插入默认空配置，JSON 列可降级再升级且不改来源或 IAM 凭据。"""
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, create_engine, inspect, select

from app.core.db import Base
from app.models import HcsAccount

from .test_migrations import _cfg


def insert_legacy_tenant(engine, code):
    table = Table("hcs_account", MetaData(), autoload_with=engine)
    now = datetime(2026, 11, 7, tzinfo=UTC)
    values = dict(code=code, name=code, short_name=code, color="#1d63f0", iam_endpoint="https://iam.example.invalid",
                  domain_name="domain", auth_type="PASSWORD", secret_cipher="unchanged-iam", endpoints={},
                  enabled=True, report_mail_to=[], metrics_source="PROMETHEUS", capabilities={}, last_check={},
                  created_at=now, updated_at=now)
    with engine.begin() as connection:
        return connection.execute(table.insert().values(**values)).inserted_primary_key[0]


def test_revision_0021_extends_0020_and_remains_in_single_head_chain(migration_url):
    scripts = ScriptDirectory.from_config(_cfg(migration_url))
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert "0021" in {revision.revision for revision in scripts.walk_revisions(base="0021", head=heads[0])}
    assert scripts.get_revision("0021").down_revision == "0020"


def test_0021_defaults_preserve_tenants_and_survive_downgrade_upgrade(migration_url):
    cfg = _cfg(migration_url)
    command.upgrade(cfg, "0020")
    engine = create_engine(migration_url)
    try:
        assert "manageone_config" not in {col["name"] for col in inspect(engine).get_columns("hcs_account")}
        old_id = insert_legacy_tenant(engine, "legacy")
        command.upgrade(cfg, "0021")
        column = next(col for col in inspect(engine).get_columns("hcs_account") if col["name"] == "manageone_config")
        assert column["nullable"] is False and column["default"] is not None
        assert isinstance(column["type"], sa.JSON)
        new_id = insert_legacy_tenant(engine, "new")  # 旧代码不写新列，同样获得服务端 {} 默认值。
        table = Table("hcs_account", MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            rows = connection.execute(select(table).order_by(table.c.id)).mappings().all()
            assert [row["manageone_config"] for row in rows] == [{}, {}]
            assert all(row["metrics_source"] == "PROMETHEUS" and row["secret_cipher"] == "unchanged-iam" for row in rows)
            connection.execute(table.update().where(table.c.id == old_id).values(manageone_config={
                "enabled": False, "endpoint": "http://192.0.2.1:8080/base", "secret_cipher": "synthetic-cipher"}))
        command.downgrade(cfg, "0020")
        assert "manageone_config" not in {col["name"] for col in inspect(engine).get_columns("hcs_account")}
        table = Table("hcs_account", MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            rows = connection.execute(select(table).order_by(table.c.id)).mappings().all()
            assert [row["id"] for row in rows] == [old_id, new_id]
            assert all(row["metrics_source"] == "PROMETHEUS" and row["secret_cipher"] == "unchanged-iam" for row in rows)
        command.upgrade(cfg, "0021")
        table = Table("hcs_account", MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            assert list(connection.scalars(select(table.c.manageone_config))) == [{}, {}]
    finally:
        engine.dispose()


def test_orm_json_default_is_fresh_per_tenant(mem_db):
    tenants = [HcsAccount(code=f"default-{i}", name=f"default-{i}", iam_endpoint="https://iam.example.invalid",
                          domain_name="domain") for i in (1, 2)]
    mem_db.add_all(tenants)
    mem_db.flush()
    assert all(tenant.manageone_config == {} for tenant in tenants)
    assert tenants[0].manageone_config is not tenants[1].manageone_config
    column = Base.metadata.tables["hcs_account"].c.manageone_config
    assert column.nullable is False and column.server_default.arg == "{}"
