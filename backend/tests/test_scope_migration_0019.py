"""迁移 0019：resource.scope_excluded（采集范围的物化标记）——旧数据默认不在范围外，可降级、可再升级。"""
from datetime import UTC, datetime

from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text

from .test_migrations import _cfg

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def add_resource(engine, name):
    table = Table("resource", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        return connection.execute(table.insert().values(
            service="dcs", resource_type="dcs_instance", resource_id=name, name=name, tenant_id=1, project_ref=None,
            health="OK", labels={}, attrs={}, metrics={}, expected_state={}, monitored=True, critical=False,
            lifecycle="ACTIVE", first_seen=NOW, last_seen=NOW)).inserted_primary_key[0]


def flag_values(engine):
    with engine.connect() as connection:
        return [bool(v) for v in connection.execute(text("SELECT scope_excluded FROM resource ORDER BY id")).scalars()]


def test_existing_resources_default_to_in_scope_and_the_column_can_be_dropped_again(migration_url):
    cfg = _cfg(migration_url)
    command.upgrade(cfg, "0018")
    engine = create_engine(migration_url)
    assert "scope_excluded" not in {c["name"] for c in inspect(engine).get_columns("resource")}
    add_resource(engine, "before-upgrade")

    command.upgrade(cfg, "0019")
    column = next(c for c in inspect(engine).get_columns("resource") if c["name"] == "scope_excluded")
    assert column["nullable"] is False
    assert flag_values(engine) == [False]                          # 旧记录默认在范围内：升级不改变任何采集行为

    add_resource(engine, "after-upgrade")                          # 不写该列的插入（旧代码路径）同样得到 false
    assert flag_values(engine) == [False, False]
    with engine.begin() as connection:
        connection.execute(text("UPDATE resource SET scope_excluded = :flag WHERE resource_id = 'after-upgrade'"),
                           {"flag": True})
    assert flag_values(engine) == [False, True]

    command.downgrade(cfg, "0018")
    assert "scope_excluded" not in {c["name"] for c in inspect(engine).get_columns("resource")}
    with engine.connect() as connection:
        names = list(connection.execute(select(Table("resource", MetaData(), autoload_with=engine).c.name)).scalars())
    assert sorted(names) == ["after-upgrade", "before-upgrade"]     # 降级只删列，不丢记录

    command.upgrade(cfg, "head")
    assert flag_values(engine) == [False, False]                   # 再次升级回到默认值，标记由下一次巡检重新计算
    engine.dispose()
