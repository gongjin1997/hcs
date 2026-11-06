"""DCS 0012 迁移只在临时测试库运行。"""
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.core.db import Base


def test_dcs_native_sample_migration_roundtrip_and_metadata(migration_url):
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.attributes["database_url"] = migration_url
    command.upgrade(config, "0011")
    engine = create_engine(migration_url)
    assert "dcs_metric_sample" not in inspect(engine).get_table_names()
    command.upgrade(config, "0012")
    assert "dcs_metric_sample" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={
            "include_object": lambda obj, name, type_, reflected, compare_to:
                name.startswith("dcs_") if type_ == "table" else True})
        assert compare_metadata(context, Base.metadata) == []
    command.downgrade(config, "0011")
    assert "dcs_metric_sample" not in inspect(engine).get_table_names()
    assert "dcs_memory_sample" in inspect(engine).get_table_names()
    command.upgrade(config, "0012")
    assert "dcs_metric_sample" in inspect(engine).get_table_names()
    engine.dispose()
