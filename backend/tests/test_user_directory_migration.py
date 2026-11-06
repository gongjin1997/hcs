"""0008 使用真实 0007 表回填，不借当前 ORM 默认值伪造历史数据库。"""
import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import MetaData, Table, create_engine, event, inspect, select
from sqlalchemy.exc import IntegrityError

from app.core.db import Base

BACKEND = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


@pytest.fixture()
def legacy_directory(migration_url):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.attributes["database_url"] = migration_url
    command.upgrade(cfg, "0007")
    engine = create_engine(migration_url)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
    metadata = MetaData()
    tables = {name: Table(name, metadata, autoload_with=engine) for name in (
        "sys_user", "contact", "contact_group", "resource", "sys_setting", "sys_role", "dcs_memory_sample",
    )}
    try:
        yield cfg, engine, tables
    finally:
        engine.dispose()


def add_user(connection, tables, username, **changes):
    values = dict(username=username, display_name=username, email=None, password_hash="legacy-hash",
                  enabled=True, failed_attempts=0, token_version=7, role_id=None, tenant_ids=None,
                  created_at=NOW, updated_at=NOW)
    values.update(changes)
    return connection.execute(tables["sys_user"].insert().values(**values)).inserted_primary_key[0]


def add_contact(connection, tables, cid, name, email, **changes):
    values = dict(id=cid, name=name, email=email, phone=None, enabled=True)
    values.update(changes)
    connection.execute(tables["contact"].insert().values(**values))


def add_resource(connection, tables, owner, tenant=101, **changes):
    values = dict(service="vpc", resource_type="vpc", resource_id=f"{owner or 'empty'}-{tenant}", name="原资源",
                  tenant_id=tenant, project_ref=None, owner=owner, health="UNKNOWN", labels={}, attrs={},
                  metrics={}, expected_state={}, monitored=True, critical=False, lifecycle="ACTIVE",
                  first_seen=NOW, last_seen=NOW)
    values.update(changes)
    return connection.execute(tables["resource"].insert().values(**values)).inserted_primary_key[0]


def rows(engine, name):
    table = Table(name, MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(select(table).order_by(*table.primary_key)).mappings()]


def migration_module():
    spec = importlib.util.spec_from_file_location(
        "directory_migration", BACKEND / "migrations/versions/20261102_0008_unified_user_directory.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contacts_use_unique_free_casefold_email_and_authoritative_users(legacy_directory):
    cfg, engine, tables = legacy_directory
    with engine.begin() as connection:
        role = connection.scalar(select(tables["sys_role"].c.id).where(tables["sys_role"].c.code == "oncall"))
        authoritative = add_user(connection, tables, "real", display_name="用户名称", email="Real@Example.com",
                                 role_id=role, tenant_ids=[101])
        disabled = add_user(connection, tables, "disabled", email="disabled@example.com", enabled=False)
        quiet = add_user(connection, tables, "quiet", email="quiet@example.com")
        add_user(connection, tables, "duplicate-a", email="shared@example.com")
        add_user(connection, tables, "duplicate-b", email="SHARED@example.com")
        free = add_user(connection, tables, "one-free", email="free@example.com")
        unicode_user = add_user(connection, tables, "unicode", email="Straße@example.com")
        no_email = add_user(connection, tables, "no-email")
        add_user(connection, tables, "legacy_contact_40")
        add_contact(connection, tables, 11, "不同旧名称", " real@example.COM ", phone="13800000001")
        add_contact(connection, tables, 12, "原通知启用", "disabled@example.com")
        add_contact(connection, tables, 13, "原通知关闭", "quiet@example.com", enabled=False)
        add_contact(connection, tables, 20, "邮箱歧义", "shared@example.com")
        add_contact(connection, tables, 30, "首个", "free@example.com")
        add_contact(connection, tables, 31, "不可抢占", "FREE@example.com")
        add_contact(connection, tables, 40, "仅登记人员", "unmatched@example.com", phone="13900000001")
        add_contact(connection, tables, 41, "已禁用联系人", "off@example.com", enabled=False)
        add_contact(connection, tables, 42, "Unicode", "STRASSE@example.com")
        add_contact(connection, tables, 43, "无邮箱", "")
        connection.execute(tables["contact_group"].insert().values(
            code="legacy", name="原组", member_ids=[40, 11, 20, 11, 999], leader_ids=[12, 41]))
    original_users = {u["id"]: u for u in rows(engine, "sys_user")}
    original_groups = rows(engine, "contact_group")
    command.upgrade(cfg, "head")
    users = {u["id"]: u for u in rows(engine, "sys_user")}
    contacts = {c["id"]: c for c in rows(engine, "contact")}
    assert {11, 12, 13, 20, 30, 31, 40, 41, 42, 43} <= set(contacts)
    assert all(sum(c["user_id"] == u["id"] for c in contacts.values()) == 1
               for u in users.values() if u["email"])
    assert rows(engine, "contact_group") == original_groups
    assert contacts[11] == dict(id=11, user_id=authoritative, name="用户名称", email="Real@Example.com",
                               phone="13800000001", enabled=True)
    assert users[authoritative]["phone"] == "13800000001"
    assert contacts[12]["user_id"] == disabled and contacts[12]["enabled"] is False
    assert users[disabled]["notify_enabled"] is True and users[disabled]["enabled"] is False
    assert contacts[13]["user_id"] == quiet and users[quiet]["enabled"] is True
    assert users[quiet]["notify_enabled"] is False
    assert contacts[30]["user_id"] == free and contacts[31]["user_id"] != free
    assert contacts[42]["user_id"] == unicode_user
    assert users[no_email]["notify_enabled"] is True and users[no_email]["email"] is None
    for cid in (20, 31, 40, 41, 43):
        user = users[contacts[cid]["user_id"]]
        assert user["id"] not in original_users
        assert user["login_enabled"] is False and user["password_hash"] == "!"
        assert user["role_id"] is None and user["tenant_ids"] is None
        assert user["enabled"] is (cid != 41) and user["notify_enabled"] is (cid != 41)
        assert user["token_version"] == 0
    assert users[contacts[40]["user_id"]]["username"] != "legacy_contact_40"
    assert users[contacts[40]["user_id"]]["phone"] == "13900000001"
    for uid, old in original_users.items():
        assert {key: users[uid][key] for key in old} == old
        assert users[uid]["login_enabled"] is True
    with engine.begin() as connection:
        report = migration_module().reconcile(connection)
    assert report["created_user_ids"] == []
    assert rows(engine, "sys_user") == list(users.values())
    assert rows(engine, "contact") == list(contacts.values())


def test_existing_phone_is_never_overwritten_by_contact(legacy_directory):
    cfg, engine, tables = legacy_directory
    with engine.begin() as connection:
        uid = add_user(connection, tables, "existing", email="match@example.com")
        add_contact(connection, tables, 1, "旧名", "match@example.com", phone="contact-phone")
    # 首次升级回填空电话；已关联用户之后的修改始终优先。
    command.upgrade(cfg, "head")
    user = Table("sys_user", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(user.update().where(user.c.id == uid).values(phone="user-phone", display_name="新名"))
        migration_module().reconcile(connection)
    assert rows(engine, "sys_user")[0]["phone"] == "user-phone"
    assert rows(engine, "contact")[0]["phone"] == "user-phone"
    assert rows(engine, "contact")[0]["name"] == "新名"


def test_owners_match_only_unique_scoped_names_and_preserve_ambiguous_text(legacy_directory):
    cfg, engine, tables = legacy_directory
    dictionary = {"owners": ["字典独有", {"name": "资源新人", "remark": "原备注"}, "同名", "字典独有"],
                  "business": [{"name": "业务", "remark": "保留"}], "enforce": True, "other": [1, 2]}
    with engine.begin() as connection:
        scoped_a = add_user(connection, tables, "scoped-a", display_name="同名", tenant_ids=[101])
        scoped_b = add_user(connection, tables, "scoped-b", display_name="同名", tenant_ids=[202])
        global_user = add_user(connection, tables, "global-user", display_name="全局人员")
        add_user(connection, tables, "duplicate-a", display_name="歧义")
        add_user(connection, tables, "duplicate-b", display_name="歧义")
        add_user(connection, tables, "cross-name", display_name="别名")
        add_user(connection, tables, "other-name", display_name="cross-name")
        a = add_resource(connection, tables, "同名", tenant=101, resource_id="scoped-a")
        b = add_resource(connection, tables, "同名", tenant=202, resource_id="scoped-b")
        ambiguous = add_resource(connection, tables, "歧义")
        cross = add_resource(connection, tables, "cross-name")
        outside = add_resource(connection, tables, "scoped-a", tenant=202)
        orphan_known = add_resource(connection, tables, "全局人员", tenant=None)
        orphan_scoped = add_resource(connection, tables, "同名", tenant=None, resource_id="orphan-scoped")
        fresh_a = add_resource(connection, tables, "资源新人", tenant=101, resource_id="fresh-a")
        fresh_b = add_resource(connection, tables, "资源新人", tenant=202, resource_id="fresh-b")
        fresh_global = add_resource(connection, tables, "无归属新人", tenant=None)
        add_resource(connection, tables, "无归属新人", tenant=101, resource_id="mixed-global")
        add_resource(connection, tables, None)
        connection.execute(tables["sys_setting"].insert().values(
            key="ledger_dict", value=dictionary, updated_by="admin", updated_at=NOW))
    original_resources = rows(engine, "resource")
    original_setting = rows(engine, "sys_setting")
    command.upgrade(cfg, "head")
    resources = {r["id"]: r for r in rows(engine, "resource")}
    users = {u["id"]: u for u in rows(engine, "sys_user")}
    assert resources[a]["owner_user_id"] == scoped_a
    assert resources[b]["owner_user_id"] == scoped_b
    assert resources[orphan_known]["owner_user_id"] == global_user
    for rid in (ambiguous, cross, outside, orphan_scoped):
        assert resources[rid]["owner_user_id"] is None
    assert resources[fresh_a]["owner_user_id"] == resources[fresh_b]["owner_user_id"]
    new_owner = users[resources[fresh_a]["owner_user_id"]]
    assert new_owner["tenant_ids"] == [101, 202]
    assert users[resources[fresh_global]["owner_user_id"]]["tenant_ids"] is None
    dictionary_user, = (u for u in users.values() if u["display_name"] == "字典独有")
    assert dictionary_user["tenant_ids"] == []
    for user in (new_owner, dictionary_user, users[resources[fresh_global]["owner_user_id"]]):
        assert user["login_enabled"] is False and user["role_id"] is None and user["password_hash"] == "!"
        assert user["email"] is None and user["notify_enabled"] is False
    assert [{k: r[k] for k in original_resources[0]} for r in resources.values()] == original_resources
    assert rows(engine, "sys_setting") == original_setting
    with engine.begin() as connection:
        report = migration_module().reconcile(connection)
    assert report["created_user_ids"] == []
    assert set(report["ambiguous_resource_ids"]) == {ambiguous, cross}
    assert set(report["out_of_scope_resource_ids"]) == {outside, orphan_scoped}
    assert report["ambiguous_dictionary_positions"] == [2]


def test_schema_matches_models_and_enforces_named_relationships(legacy_directory):
    cfg, engine, tables = legacy_directory
    with engine.begin() as connection:
        uid = add_user(connection, tables, "linked", email="linked@example.com")
        add_contact(connection, tables, 77, "linked", "linked@example.com")
        rid = add_resource(connection, tables, "linked")
        connection.execute(tables["dcs_memory_sample"].insert().values(
            resource_ref=rid, tenant_id=101, observed_at=NOW, memory_usage=42))
    original_sample = rows(engine, "dcs_memory_sample")
    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("sys_user")}
    assert columns["phone"]["type"].length == 32 and columns["phone"]["nullable"]
    for field in ("login_enabled", "notify_enabled"):
        assert not columns[field]["nullable"] and columns[field]["default"] is not None
    for table, column, index, constraint, deletion, unique in (
        ("contact", "user_id", "ix_contact_user_id", "fk_contact_user_id_sys_user", "RESTRICT", True),
        ("resource", "owner_user_id", "ix_resource_owner_user_id", "fk_resource_owner_user_id_sys_user", "SET NULL", False),
    ):
        indexes = {i["name"]: i for i in inspector.get_indexes(table)}
        assert indexes[index]["column_names"] == [column] and bool(indexes[index]["unique"]) is unique
        fk, = [fk for fk in inspector.get_foreign_keys(table) if fk["constrained_columns"] == [column]]
        assert fk["name"] == constraint and fk["referred_table"] == "sys_user" and fk["referred_columns"] == ["id"]
        assert fk["options"]["ondelete"] == deletion
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    assert rows(engine, "dcs_memory_sample") == original_sample
    users = Table("sys_user", MetaData(), autoload_with=engine)
    contacts = Table("contact", MetaData(), autoload_with=engine)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(users.delete().where(users.c.id == uid))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(contacts.insert().values(name="duplicate", email="x@example.com", enabled=True, user_id=uid))
    with engine.begin() as connection:
        connection.execute(contacts.delete().where(contacts.c.user_id == uid))
        connection.execute(users.delete().where(users.c.id == uid))
    assert rows(engine, "resource")[0]["owner_user_id"] is None
    assert rows(engine, "resource")[0]["owner"] == "linked"


def test_downgrade_preserves_users_snapshots_and_links_on_reupgrade(legacy_directory):
    cfg, engine, tables = legacy_directory
    with engine.begin() as connection:
        role = connection.scalar(select(tables["sys_role"].c.id).where(tables["sys_role"].c.code == "oncall"))
        uid = add_user(connection, tables, "account", email="a@example.com", role_id=role)
        add_contact(connection, tables, 19, "旧名", "a@example.com", phone="13800000000")
        add_contact(connection, tables, 29, "重复邮箱一", "duplicate@example.com")
        add_contact(connection, tables, 39, "重复邮箱二", "duplicate@example.com")
        add_contact(connection, tables, 49, "缺少邮箱", "")
        add_resource(connection, tables, "仅负责人", tenant=101)
        connection.execute(tables["contact_group"].insert().values(
            code="stable", name="组", member_ids=[19, 29, 39, 49], leader_ids=[29]))
    command.upgrade(cfg, "head")
    users = Table("sys_user", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(users.update().where(users.c.id == uid).values(login_enabled=False, token_version=8))
    before_users = rows(engine, "sys_user")
    before_contacts, before_groups, before_resources = (rows(engine, name) for name in ("contact", "contact_group", "resource"))
    command.downgrade(cfg, "0007")
    assert not {"phone", "login_enabled", "notify_enabled"} & {c["name"] for c in inspect(engine).get_columns("sys_user")}
    assert "user_id" not in {c["name"] for c in inspect(engine).get_columns("contact")}
    assert not {"owner_user_id", "scope_excluded"} & {c["name"] for c in inspect(engine).get_columns("resource")}
    assert not inspect(engine).get_foreign_keys("contact")
    assert "ix_contact_user_id" not in {i["name"] for i in inspect(engine).get_indexes("contact")}
    assert "ix_resource_owner_user_id" not in {i["name"] for i in inspect(engine).get_indexes("resource")}
    assert rows(engine, "contact") == [{k: v for k, v in c.items() if k != "user_id"} for c in before_contacts]
    assert rows(engine, "contact_group") == before_groups
    assert rows(engine, "resource") == [
        {k: v for k, v in r.items() if k not in {"owner_user_id", "scope_excluded"}} for r in before_resources]
    retained = {u["id"]: u for u in rows(engine, "sys_user")}
    assert set(retained) == {u["id"] for u in before_users}
    assert all(u["enabled"] is False for u in retained.values())
    assert retained[uid]["role_id"] == role and retained[uid]["token_version"] >= 8
    command.upgrade(cfg, "head")
    restored = rows(engine, "sys_user")
    assert [u["id"] for u in restored] == [u["id"] for u in before_users]
    for old, user in zip(before_users, restored, strict=True):
        for field in ("username", "phone", "login_enabled", "notify_enabled", "password_hash", "role_id", "tenant_ids"):
            assert user[field] == old[field]
        assert user["enabled"] is False
    assert [c["user_id"] for c in rows(engine, "contact")] == [c["user_id"] for c in before_contacts]
    assert rows(engine, "contact_group") == before_groups
    assert rows(engine, "resource") == before_resources
    assert rows(engine, "sys_setting") == []
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
