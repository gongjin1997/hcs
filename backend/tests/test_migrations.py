"""数据库迁移：0001 → 0002（租户 / 角色权限 / 审计 / 容器事件）升级、数据迁移（管理员 → 平台管理员角色、tenant_id 回填）、
模型与迁移一致（等价于 alembic check）、可降级。"""
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from app.core.db import Base

BACKEND = Path(__file__).resolve().parents[1]


def _cfg(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.attributes["database_url"] = url
    return cfg


def test_upgrade_migrates_data_and_matches_models(tmp_path):
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    cfg = _cfg(url)
    command.upgrade(cfg, "0001")
    engine = create_engine(url)
    now = "2026-10-01 00:00:00"
    with engine.begin() as c:            # 0001 时代的数据：单账号 + 管理员 / 成员两种身份
        c.execute(text("INSERT INTO hcs_account (id, name, iam_endpoint, domain_name, auth_type, secret_cipher, endpoints,"
                       " enabled, created_at, updated_at) VALUES (1, '生产账号', 'https://iam.example.com', 'prod',"
                       " 'PASSWORD', 'x', '{}', 1, :t, :t)"), {"t": now})
        for i, (name, is_admin) in enumerate((("admin", 1), ("zhangwei", 0)), start=1):
            c.execute(text("INSERT INTO sys_user (id, username, display_name, password_hash, is_admin, enabled,"
                           " failed_attempts, created_at, updated_at) VALUES (:i, :n, :n, 'h', :a, 1, 0, :t, :t)"),
                      {"i": i, "n": name, "a": is_admin, "t": now})
    command.upgrade(cfg, "head")
    insp = inspect(engine)
    assert {"sys_role", "audit_log", "pod_event", "pod_state", "pod_probe"} <= set(insp.get_table_names())
    assert "is_admin" not in {col["name"] for col in insp.get_columns("sys_user")}
    with engine.connect() as c:
        roles = dict(c.execute(text("SELECT u.username, r.code FROM sys_user u JOIN sys_role r ON r.id = u.role_id")).all())
        assert roles == {"admin": "admin", "zhangwei": "oncall"}
        assert c.execute(text("SELECT code FROM hcs_account")).scalar() == "tenant-1"
        assert c.execute(text("SELECT count(*) FROM sys_role WHERE builtin = 1")).scalar() == 4
        diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
    assert diff == []
    command.downgrade(cfg, "0001")
    with engine.connect() as c:
        assert dict(c.execute(text("SELECT username, is_admin FROM sys_user")).all()) == {"admin": 1, "zhangwei": 0}
    command.upgrade(cfg, "head")
    engine.dispose()
