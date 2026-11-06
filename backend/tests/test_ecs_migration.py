"""ECS 指标迁移在 SQLite / PostgreSQL 下的升级、配置保留和降级验证。"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select


def test_ecs_migration_preserves_disabled_custom_tasks_and_rollback_choice(migration_url):
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "migrations"))
    cfg.attributes["database_url"] = migration_url
    command.upgrade(cfg, "0010")
    engine = create_engine(migration_url)
    task = Table("inspection_task", MetaData(), autoload_with=engine)
    with engine.begin() as conn:
        conn.execute(task.insert().values(code="ecs_status", name="保留原任务", service="ecs", interval_sec=600,
            cron="0 1 * * *", enabled=False, params={"aom": None}))
    command.upgrade(cfg, "head")
    assert "ecs_metric_sample" in inspect(engine).get_table_names()
    with engine.begin() as conn:
        rows = {row["code"]: row for row in conn.execute(select(task)).mappings()}
        assert rows["ecs_status"]["name"] == "保留原任务"
        assert rows["ecs_metrics"]["enabled"] is False
        assert rows["ecs_metrics"]["interval_sec"] == 600 and rows["ecs_metrics"]["cron"] == "0 1 * * *"
        conn.execute(task.update().where(task.c.code == "ecs_metrics").values(interval_sec=900, cron=None, enabled=True))
    command.downgrade(cfg, "0010")
    assert "ecs_metric_sample" not in inspect(engine).get_table_names()
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        restored = conn.execute(select(task).where(task.c.code == "ecs_metrics")).mappings().one()
        assert restored["interval_sec"] == 900 and restored["enabled"] is True and restored["cron"] is None
        assert conn.scalar(select(task.c.enabled).where(task.c.code == "ecs_status")) is False
    engine.dispose()
