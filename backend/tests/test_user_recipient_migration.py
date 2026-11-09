"""0009 的真实迁移：旧邮件配置转人员引用，降级使用人员最新邮箱。"""
import importlib.util
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import MetaData, Table, create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine

from app.core.db import Base

BACKEND = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


@pytest.fixture()
def legacy_recipients(migration_url):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.attributes["database_url"] = migration_url
    command.upgrade(cfg, "0008")

    def enable_foreign_keys(connection, _record):
        if isinstance(connection, sqlite3.Connection):
            connection.execute("PRAGMA foreign_keys=ON")

    # 同时覆盖 Alembic env.py 新建的连接，不能只给断言用的 engine 开外键。
    event.listen(Engine, "connect", enable_foreign_keys)
    engine = create_engine(migration_url)
    metadata = MetaData()
    tables = {name: Table(name, metadata, autoload_with=engine) for name in (
        "sys_user", "contact", "contact_group", "hcs_account", "hcs_project", "sys_setting",
    )}
    try:
        yield cfg, engine, tables
    finally:
        engine.dispose()
        event.remove(Engine, "connect", enable_foreign_keys)


def add_user(connection, tables, uid, email, *, scope=None, **changes):
    values = dict(id=uid, username=f"user-{uid}", display_name=f"人员{uid}", email=email, phone=None,
                  password_hash="legacy-hash", role_id=None, tenant_ids=scope, enabled=True,
                  login_enabled=True, notify_enabled=True, token_version=3, failed_attempts=0,
                  created_at=NOW, updated_at=NOW)
    values.update(changes)
    connection.execute(tables["sys_user"].insert().values(**values))


def add_account(connection, tables, tid, recipients):
    connection.execute(tables["hcs_account"].insert().values(
        id=tid, code=f"tenant-{tid}", name=f"租户{tid}", short_name="", color="#123456",
        iam_endpoint="https://iam.invalid", domain_name="test", auth_type="PASSWORD", secret_cipher="",
        endpoints={}, enabled=True, report_mail_to=recipients, metrics_source="NONE", capabilities={},
        last_check={}, created_at=NOW, updated_at=NOW))
    connection.execute(tables["hcs_project"].insert().values(
        account_id=tid, region_id="test", project_id=f"p-{tid}", external_domain="test.invalid",
        env="PROD", workspaces=[], enabled=True))


def add_setting(connection, tables, key, value):
    connection.execute(tables["sys_setting"].insert().values(
        key=key, value=value, updated_by="operator", updated_at=NOW))


def rows(engine, name):
    table = Table(name, MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(select(table).order_by(*table.primary_key)).mappings()]


def migration_module():
    spec = importlib.util.spec_from_file_location(
        "recipient_migration", BACKEND / "migrations/versions/20261102_0009_user_recipient_references.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_uses_unique_scoped_casefold_users_without_changing_existing_directory(legacy_recipients):
    cfg, engine, tables = legacy_recipients
    with engine.begin() as connection:
        add_user(connection, tables, 100, "Global@Example.com")
        add_user(connection, tables, 200, "scoped@example.com", scope=[101])
        add_user(connection, tables, 300, "scoped@example.com", scope=[202])
        add_user(connection, tables, 400, "Straße@example.com")
        add_user(connection, tables, 500, "disabled@example.com", enabled=False)
        add_user(connection, tables, 600, "quiet@example.com", notify_enabled=False)
        add_account(connection, tables, 101, [" global@example.COM ", "scoped@example.com", "GLOBAL@example.com",
                                             "disabled@example.com", "quiet@example.com", "STRASSE@example.com"])
        add_account(connection, tables, 202, ["scoped@example.com"])
        add_account(connection, tables, 303, [])
        connection.execute(tables["contact"].insert().values(
            id=700, user_id=100, name="保留的联系人快照", email="old@example.com", phone="123", enabled=False))
        connection.execute(tables["contact_group"].insert().values(
            id=900, code="legacy", name="原通知组", member_ids=[700, 700, 999], leader_ids=[700]))
        add_setting(connection, tables, "report", {"mail_to": ["global@example.com"], "cron": {"DAILY": "0 7 * * *"},
                                                  "opaque": {"keep": [1, 2]}})
        add_setting(connection, tables, "platform_alarm", {"mail_to": ["STRASSE@example.com"], "enabled": False})
    original_users = rows(engine, "sys_user")
    original_contacts, original_groups = rows(engine, "contact"), rows(engine, "contact_group")
    original_projects = rows(engine, "hcs_project")
    old_accounts, old_settings = rows(engine, "hcs_account"), rows(engine, "sys_setting")
    command.upgrade(cfg, "head")
    accounts = {row["id"]: row for row in rows(engine, "hcs_account")}
    assert accounts[101]["report_user_ids"] == [100, 200, 500, 600, 400]
    assert accounts[202]["report_user_ids"] == [300]
    assert accounts[303]["report_user_ids"] == []
    assert rows(engine, "sys_user") == original_users
    assert rows(engine, "contact_group") == original_groups
    assert rows(engine, "contact")[0] == original_contacts[0]
    assert rows(engine, "hcs_project") == original_projects
    for old, new in zip(old_accounts, accounts.values(), strict=True):
        assert {key: new[key] for key in old} == old
    settings = {row["key"]: row for row in rows(engine, "sys_setting")}
    assert settings["report"]["value"]["recipient_user_ids"] == [100]
    assert settings["platform_alarm"]["value"]["recipient_user_ids"] == [400]
    for old in old_settings:
        new = settings[old["key"]]
        assert {k: v for k, v in new["value"].items() if k != "recipient_user_ids"} == old["value"]
        assert new["updated_by"] == old["updated_by"] and new["updated_at"] == old["updated_at"]

    def compare_recipient_default(_context, column, *_defaults):
        # 已发布修订中的其他服务端默认值不属于 0009；新增列须与当前模型严格一致。
        return None if column.table.name == "hcs_account" and column.name == "report_user_ids" else False

    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={
            "compare_type": True, "compare_server_default": compare_recipient_default})
        assert compare_metadata(context, Base.metadata) == []
    column, = [c for c in inspect(engine).get_columns("hcs_account") if c["name"] == "report_user_ids"]
    assert not column["nullable"] and column["default"] is not None


def test_unknown_ambiguous_and_out_of_scope_recipients_create_minimal_directory_users(legacy_recipients):
    cfg, engine, tables = legacy_recipients
    with engine.begin() as connection:
        add_user(connection, tables, 100, "ambiguous@example.com")
        add_user(connection, tables, 200, "AMBIGUOUS@example.com")
        add_user(connection, tables, 300, "outside@example.com", scope=[202])
        add_account(connection, tables, 101, ["new@example.com", "ambiguous@example.com", "outside@example.com"])
        add_account(connection, tables, 202, ["new@example.com"])
        add_setting(connection, tables, "report", {"mail_to": ["new@example.com", "outside@example.com"]})
        add_setting(connection, tables, "platform_alarm", {"mail_to": ["NEW@example.com", "outside@example.com"]})
        connection.execute(tables["contact"].insert().values(
            id=1200, user_id=None, name="不能按同邮箱抢占", email="new@example.com", enabled=True))
        connection.execute(tables["contact_group"].insert().values(
            id=1500, code="stable", name="保持原组", member_ids=[1200], leader_ids=[1200]))
    original = rows(engine, "sys_user")
    original_contact, original_group = rows(engine, "contact"), rows(engine, "contact_group")
    command.upgrade(cfg, "head")
    users = {row["id"]: row for row in rows(engine, "sys_user")}
    accounts = {row["id"]: row for row in rows(engine, "hcs_account")}
    settings = {row["key"]: row["value"] for row in rows(engine, "sys_setting")}
    assert len(users) == 9
    assert accounts[101]["report_user_ids"][0] != accounts[202]["report_user_ids"][0]
    global_ids = settings["report"]["recipient_user_ids"]
    assert global_ids == settings["platform_alarm"]["recipient_user_ids"]
    assert users[accounts[101]["report_user_ids"][0]]["tenant_ids"] == [101]
    assert users[accounts[202]["report_user_ids"][0]]["tenant_ids"] == [202]
    assert users[accounts[101]["report_user_ids"][2]]["tenant_ids"] == [101]
    assert all(users[uid]["tenant_ids"] is None for uid in global_ids)
    for old in original:
        assert users[old["id"]] == old
    created = [row for row in users.values() if row["id"] > 300]
    contacts = rows(engine, "contact")
    assert contacts[0] == original_contact[0] and rows(engine, "contact_group") == original_group
    for row in created:
        assert len(row["username"]) <= 32 and row["username"].startswith("recipient_")
        assert row["display_name"] == row["email"].split("@", 1)[0][:64]
        assert row["login_enabled"] is False and row["role_id"] is None and row["password_hash"] == "!"
        assert row["enabled"] is True and row["notify_enabled"] is True and row["phone"] is None
        projection, = [contact for contact in contacts if contact["user_id"] == row["id"]]
        assert projection["id"] > 1200 and projection["email"] == row["email"]
        assert projection["name"] == row["display_name"] and projection["enabled"] is True
    before = {name: rows(engine, name) for name in ("sys_user", "contact", "contact_group", "hcs_account", "sys_setting")}
    with engine.begin() as connection:
        report = migration_module().reconcile(connection)
    assert report["created_users"] == report["created_contacts"] == 0
    assert all(rows(engine, name) == value for name, value in before.items())


def test_reconcile_reports_safe_duplicate_identities_and_never_revives_explicit_empty_configuration(legacy_recipients):
    cfg, engine, tables = legacy_recipients
    with engine.begin() as connection:
        add_user(connection, tables, 100, "ambiguous@example.com")
        add_user(connection, tables, 200, "AMBIGUOUS@example.com")
        add_user(connection, tables, 300, "scoped@example.com", scope=[101])
        add_account(connection, tables, 101, ["old@example.com"])
        add_setting(connection, tables, "report", {"recipient_user_ids": [], "mail_to": ["ignored@example.com"], "other": 7})
    command.upgrade(cfg, "head")
    accounts = Table("hcs_account", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(accounts.update().values(report_user_ids=[]))
        add_setting(connection, tables, "platform_alarm", {"mail_to": ["ambiguous@example.com", "scoped@example.com",
                                                                     "new@example.com", "NEW@example.com"]})
        report = migration_module().reconcile(connection)
    assert report["created_users"] == 3
    assert report["created_ambiguous_users"] == 1
    assert report["created_out_of_scope_users"] == 1
    assert report["created_unknown_users"] == 1
    assert rows(engine, "hcs_account")[0]["report_user_ids"] == []
    assert next(row for row in rows(engine, "sys_setting") if row["key"] == "report")["value"] == {
        "recipient_user_ids": [], "mail_to": ["ignored@example.com"], "other": 7}
    before = rows(engine, "sys_user")
    with engine.begin() as connection:
        assert migration_module().reconcile(connection)["created_users"] == 0
    assert rows(engine, "sys_user") == before


def test_downgrade_derives_fresh_eligible_emails_and_keeps_users_projects_and_unknown_settings(legacy_recipients):
    cfg, engine, tables = legacy_recipients
    with engine.begin() as connection:
        add_user(connection, tables, 100, "old@example.com")
        add_user(connection, tables, 200, "tenant@example.com", scope=[101])
        add_user(connection, tables, 300, "disabled@example.com", enabled=False)
        add_user(connection, tables, 400, "quiet@example.com", notify_enabled=False)
        add_user(connection, tables, 500, "outside@example.com", scope=[202])
        add_user(connection, tables, 600, None)
        add_account(connection, tables, 101, ["old@example.com", "tenant@example.com"])
        add_account(connection, tables, 202, ["must-clear@example.com"])
        add_setting(connection, tables, "report", {"mail_to": ["old@example.com"], "opaque": {"a": [1, 2]}})
        add_setting(connection, tables, "platform_alarm", {"mail_to": ["will-clear@example.com"], "enabled": True})
        add_setting(connection, tables, "unrelated", {"mail_to": ["untouched@example.com"], "recipient_user_ids": [123]})
    command.upgrade(cfg, "head")
    users = Table("sys_user", MetaData(), autoload_with=engine)
    account = Table("hcs_account", MetaData(), autoload_with=engine)
    setting = tables["sys_setting"]
    with engine.begin() as connection:
        connection.execute(users.update().where(users.c.id == 100).values(email="Newest@Example.com", display_name="新名"))
        connection.execute(account.update().where(account.c.id == 101).values(
            report_user_ids=[100, 200, 300, 400, 500, 600, 99999]))
        connection.execute(account.update().where(account.c.id == 202).values(report_user_ids=[]))
        connection.execute(setting.update().where(setting.c.key == "report").values(value={
            "mail_to": ["old@example.com"], "recipient_user_ids": [100, 200, 300, 400, 600, 99999], "opaque": {"a": [1, 2]}}))
        connection.execute(setting.update().where(setting.c.key == "platform_alarm").values(value={
            "mail_to": ["will-clear@example.com"], "recipient_user_ids": [], "enabled": True}))
    before = {name: rows(engine, name) for name in ("sys_user", "contact", "contact_group", "hcs_project")}
    command.downgrade(cfg, "0008")
    assert "report_user_ids" not in {c["name"] for c in inspect(engine).get_columns("hcs_account")}
    accounts = {row["id"]: row for row in rows(engine, "hcs_account")}
    assert accounts[101]["report_mail_to"] == ["Newest@Example.com", "tenant@example.com"]
    assert accounts[202]["report_mail_to"] == []
    settings = {row["key"]: row["value"] for row in rows(engine, "sys_setting")}
    assert settings["report"] == {"mail_to": ["Newest@Example.com"], "opaque": {"a": [1, 2]}}
    assert settings["platform_alarm"] == {"mail_to": [], "enabled": True}
    assert settings["unrelated"] == {"mail_to": ["untouched@example.com"], "recipient_user_ids": [123]}
    assert all(rows(engine, name) == value for name, value in before.items())
    with engine.connect() as connection:
        if connection.dialect.name == "sqlite":
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    command.upgrade(cfg, "head")
    assert all(rows(engine, name) == value for name, value in before.items())
    assert rows(engine, "hcs_account")[0]["report_user_ids"] == [100, 200]


def test_ambiguous_directory_users_and_contact_ids_are_stable_across_upgrade_downgrade_upgrade(legacy_recipients):
    cfg, engine, tables = legacy_recipients
    with engine.begin() as connection:
        add_user(connection, tables, 100, "duplicate@example.com")
        add_user(connection, tables, 200, "duplicate@example.com")
        for tid in (101, 202):
            add_account(connection, tables, tid, ["duplicate@example.com", "new@example.com"])
        add_setting(connection, tables, "report", {"mail_to": ["duplicate@example.com", "new@example.com"]})
    command.upgrade(cfg, "head")
    before = {name: rows(engine, name) for name in ("sys_user", "contact", "hcs_account", "sys_setting")}
    # 0022 的可逆日志在重新升级时有新的时间戳；收件人业务配置必须逐字保持。
    before["sys_setting"] = [row for row in before["sys_setting"] if row["key"] != "migration:mrs-split-0022"]
    command.downgrade(cfg, "0008")
    command.upgrade(cfg, "head")
    for name, expected in before.items():
        actual = rows(engine, name)
        if name == "sys_setting":
            journal, = [row for row in actual if row["key"] == "migration:mrs-split-0022"]
            assert journal["updated_by"] == "system:mrs-split-0022" and journal["value"]["node_refs"] == []
            actual = [row for row in actual if row["key"] != "migration:mrs-split-0022"]
        assert actual == expected, name


def test_generated_username_collisions_long_display_names_and_malformed_json_are_safe(legacy_recipients):
    cfg, engine, tables = legacy_recipients
    module = migration_module()
    address = "a" * 80 + "@example.com"
    base = module._username(address, 101)
    with engine.begin() as connection:
        add_user(connection, tables, 100, "different@example.com", username=base)
        add_account(connection, tables, 101, [address, None, 123, "", "invalid", "a" * 129 + "@example.com"])
        add_setting(connection, tables, "report", ["unexpected", {"mail_to": ["keep@example.com"]}])
        add_setting(connection, tables, "platform_alarm", {"mail_to": "invalid-scalar", "unknown": [1, {"k": "v"}]})
    command.upgrade(cfg, "head")
    accounts = rows(engine, "hcs_account")
    users = {row["id"]: row for row in rows(engine, "sys_user")}
    chosen = users[accounts[0]["report_user_ids"][0]]
    assert chosen["username"] != base and len(chosen["username"]) <= 32
    assert chosen["display_name"] == "a" * 64 and chosen["email"] == address
    assert accounts[0]["report_mail_to"] == [address, None, 123, "", "invalid", "a" * 129 + "@example.com"]
    settings = {row["key"]: row["value"] for row in rows(engine, "sys_setting")}
    assert settings["report"] == ["unexpected", {"mail_to": ["keep@example.com"]}]
    assert settings["platform_alarm"] == {
        "mail_to": "invalid-scalar", "unknown": [1, {"k": "v"}], "recipient_user_ids": []}
    command.downgrade(cfg, "0008")
    command.upgrade(cfg, "head")
    assert rows(engine, "sys_user") == list(users.values())
    assert rows(engine, "hcs_account")[0]["report_user_ids"] == [chosen["id"]]


def test_downgrade_deduplicates_latest_addresses_and_handles_invalid_ids(legacy_recipients):
    cfg, engine, tables = legacy_recipients
    with engine.begin() as connection:
        add_user(connection, tables, 100, "first@example.com")
        add_user(connection, tables, 200, "second@example.com")
        add_account(connection, tables, 101, ["first@example.com", "second@example.com"])
        add_setting(connection, tables, "report", {"other": "keep"})
    command.upgrade(cfg, "head")
    users = Table("sys_user", MetaData(), autoload_with=engine)
    account = Table("hcs_account", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(users.update().where(users.c.id == 100).values(email=" SAME@example.com "))
        connection.execute(users.update().where(users.c.id == 200).values(email="same@EXAMPLE.com"))
        connection.execute(account.update().values(report_user_ids=[100, 200, 100, None, "100", True, {}, 99999]))
        connection.execute(tables["sys_setting"].update().where(tables["sys_setting"].c.key == "report").values(value={
            "recipient_user_ids": [200, 100], "other": "keep"}))
    command.downgrade(cfg, "0008")
    assert rows(engine, "hcs_account")[0]["report_mail_to"] == ["SAME@example.com"]
    assert rows(engine, "sys_setting")[0]["value"] == {"mail_to": ["same@EXAMPLE.com"], "other": "keep"}


@pytest.mark.pg
@pytest.mark.parametrize("advance", [False, True])
def test_postgres_generated_id_sequences_never_move_backwards(legacy_recipients, advance):
    cfg, engine, tables = legacy_recipients
    with engine.begin() as connection:
        add_user(connection, tables, 100, "existing@example.com")
        connection.execute(tables["contact"].insert().values(
            id=1200, user_id=100, name="existing", email="existing@example.com", enabled=True))
        add_account(connection, tables, 101, ["new@example.com"])
        if advance:
            for name, high in (("sys_user", 5000), ("contact", 7000)):
                connection.execute(text("SELECT setval(pg_get_serial_sequence(:name, 'id'), :high, true)"),
                                   {"name": name, "high": high})
    command.upgrade(cfg, "head")
    users = Table("sys_user", MetaData(), autoload_with=engine)
    contacts = Table("contact", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        values = {k: v for k, v in rows(engine, "sys_user")[0].items() if k != "id"}
        values.update(username="after-migration", email="after@example.com")
        uid = connection.execute(users.insert().values(**values)).inserted_primary_key[0]
        cid = connection.execute(contacts.insert().values(
            user_id=uid, name="after", email="after@example.com", enabled=True)).inserted_primary_key[0]
        assert uid > (5000 if advance else 101)
        assert cid > (7000 if advance else 1201)
