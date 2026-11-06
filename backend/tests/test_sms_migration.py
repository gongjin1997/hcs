"""0010 升降级：真实隔离库、电话联系人生命周期、Outbox 约束及默认关闭安全性。"""
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import MetaData, Table, create_engine, event, inspect, select
from sqlalchemy.exc import IntegrityError

from app.core.crypto import encrypt
from app.core.db import Base
from app.models import AlarmEvent, Contact, ContactGroup, HcsAccount, NotifyRoute, SysSetting, User
from app.models.sms import SmsNotification
from app.services import sms_service, sms_settings

from .test_migrations import _cfg

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
MIGRATION = Path(__file__).resolve().parents[1] / "migrations/versions/20261102_0010_sms_escalation_outbox.py"


@pytest.fixture()
def pre_sms(migration_url):
    cfg = _cfg(migration_url)
    command.upgrade(cfg, "0009")
    engine = create_engine(migration_url)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield cfg, engine
    finally:
        engine.dispose()


def _user(connection, username, **changes):
    values = {"username": username, "display_name": username, "email": None, "phone": "13800000001",
              "login_enabled": False, "notify_enabled": True, "password_hash": "!", "tenant_ids": None,
              "enabled": True, "created_at": NOW, "updated_at": NOW, **changes}
    return connection.execute(User.__table__.insert().values(**values)).inserted_primary_key[0]


def _rows(engine, name):
    table = Table(name, MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(select(table).order_by(*table.primary_key)).mappings()]


def _module():
    spec = importlib.util.spec_from_file_location("sms_outbox_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_backfills_phone_people_without_changing_identity_or_groups(pre_sms):
    cfg, engine = pre_sms
    contact = Table("contact", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        enabled = _user(connection, "phone-only", phone="+86 138-0000-0001")
        quiet = _user(connection, "quiet", phone="8613900000002", notify_enabled=False)
        disabled = _user(connection, "disabled", enabled=False)
        invalid = _user(connection, "international", phone="+12025550123")
        existing = _user(connection, "existing", email="existing@example.invalid")
        connection.execute(contact.insert().values(id=701, user_id=existing, name="原联系人", email="existing@example.invalid",
                                                   phone=None, enabled=False))
        connection.execute(ContactGroup.__table__.insert().values(code="preserved", name="原组",
                                                                  member_ids=[701], leader_ids=[701]))
        connection.execute(SysSetting.__table__.insert().values(key="untouched", value={"preserve": True}))
    users, groups, settings = (_rows(engine, name) for name in ("sys_user", "contact_group", "sys_setting"))
    tables_before = set(inspect(engine).get_table_names())
    command.upgrade(cfg, "0010")
    assert set(inspect(engine).get_table_names()) - tables_before == {"sms_notification"}
    assert _rows(engine, "sys_user") == users and _rows(engine, "contact_group") == groups
    assert _rows(engine, "sys_setting") == settings
    contacts = {row["user_id"]: row for row in _rows(engine, "contact")}
    assert contacts[existing] == dict(id=701, user_id=existing, name="原联系人", email="existing@example.invalid",
                                      phone=None, enabled=False)
    assert contacts[enabled]["id"] > 701 and contacts[enabled]["email"] == "" and contacts[enabled]["enabled"] is True
    assert contacts[enabled]["phone"] == "+86 138-0000-0001"
    assert contacts[quiet]["enabled"] is False and contacts[disabled]["enabled"] is False and invalid not in contacts
    with engine.begin() as connection:
        _module().backfill_contacts(connection)
    assert {row["user_id"]: row for row in _rows(engine, "contact")} == contacts


def test_new_table_matches_models_and_has_expected_foreign_keys_indexes(pre_sms):
    cfg, engine = pre_sms
    command.upgrade(cfg, "0010")
    inspector = inspect(engine)
    indexes = {index["name"]: index for index in inspector.get_indexes("sms_notification")}
    assert indexes["uk_sms_notification_dedup"]["unique"]
    assert {"ix_sms_notification_tenant_id", "ix_sms_notification_alarm_id", "ix_sms_notification_user_id"} <= set(indexes)
    foreign_keys = {fk["constrained_columns"][0]: fk for fk in inspector.get_foreign_keys("sms_notification")}
    assert foreign_keys["tenant_id"]["options"]["ondelete"] == "CASCADE"
    assert foreign_keys["alarm_id"]["options"]["ondelete"] == "CASCADE"
    assert foreign_keys["user_id"]["options"]["ondelete"] == "SET NULL"
    # 0010 的表结构仍按本版本断言；完整模型比较使用当前迁移头，允许后续增加独立表。
    command.upgrade(cfg, "head")
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def _outbox_records(engine):
    with engine.begin() as connection:
        tenant = connection.execute(HcsAccount.__table__.insert().values(code="sms-test", name="合成租户",
            iam_endpoint="https://example.invalid", domain_name="synthetic")).inserted_primary_key[0]
        user = _user(connection, "sms-recipient")
        alarm = connection.execute(AlarmEvent.__table__.insert().values(tenant_id=tenant, fingerprint="sms-migration",
            rule_id="SMS-TEST", service="ecs", resource_type="ecs_server", resource_name="合成资源", severity="P1",
            status="FIRING", title="合成告警", check_item="status")).inserted_primary_key[0]
        values = {"tenant_id": tenant, "user_id": user, "alarm_id": alarm, "alarm_generation": 0,
                  "dedup_key": "a" * 64, "config_revision": 1, "route_id": 1, "after_min": 15, "content": "安全短信",
                  "phone_mask": "138****0001", "created_at": NOW}
        connection.execute(SmsNotification.__table__.insert().values(**values))
    return tenant, user, alarm, values


def test_outbox_uniqueness_set_null_and_cascades(pre_sms):
    cfg, engine = pre_sms
    command.upgrade(cfg, "0010")
    tenant, user, alarm, values = _outbox_records(engine)
    with engine.begin() as connection:
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(SmsNotification.__table__.insert().values(**values))
        connection.execute(User.__table__.delete().where(User.id == user))
        assert connection.scalar(select(SmsNotification.user_id)) is None
        connection.execute(AlarmEvent.__table__.delete().where(AlarmEvent.id == alarm))
        assert connection.execute(select(SmsNotification.id)).first() is None
        alarm = connection.execute(AlarmEvent.__table__.insert().values(tenant_id=tenant, fingerprint="sms-second",
            rule_id="SMS-TEST", service="ecs", resource_type="ecs_server", resource_name="合成", severity="P1",
            status="FIRING", title="合成", check_item="status")).inserted_primary_key[0]
        connection.execute(SmsNotification.__table__.insert().values(**{**values, "user_id": None,
                                                                      "alarm_id": alarm, "dedup_key": "b" * 64}))
        connection.execute(HcsAccount.__table__.delete().where(HcsAccount.id == tenant))
        assert connection.execute(select(SmsNotification.id)).first() is None


def test_downgrade_drops_sms_logs_closes_existing_switch_and_reupgrade_preserves_profiles(pre_sms):
    cfg, engine = pre_sms
    with engine.begin() as connection:
        user = _user(connection, "phone-only")
        global_config = {"enabled": True, "enabled_since": NOW.isoformat(), "preserve": "unchanged"}
        tenant_config = {"enabled": True, "revision": 8, "preserve": "unchanged"}
        connection.execute(SysSetting.__table__.insert().values(key="sms", value=global_config))
        connection.execute(SysSetting.__table__.insert().values(key="sms_tenant:101", value=tenant_config))
    command.upgrade(cfg, "0010")
    created, = [row for row in _rows(engine, "contact") if row["user_id"] == user]
    assert created["enabled"] is True
    command.downgrade(cfg, "0009")
    assert "sms_notification" not in inspect(engine).get_table_names()
    assert [row for row in _rows(engine, "contact") if row["user_id"] == user][0]["enabled"] is False
    settings = {row["key"]: row["value"] for row in _rows(engine, "sys_setting")}
    assert settings["sms"] == {**global_config, "enabled": False} and settings["sms_tenant:101"] == tenant_config
    command.upgrade(cfg, "0010")
    restored, = [row for row in _rows(engine, "contact") if row["user_id"] == user]
    assert restored == created
    assert {row["key"]: row["value"] for row in _rows(engine, "sys_setting")}["sms"]["enabled"] is False
    assert "永久删除" in _module().__doc__


def test_downgrade_does_not_create_sms_config_when_absent(pre_sms):
    cfg, engine = pre_sms
    command.upgrade(cfg, "0010")
    command.downgrade(cfg, "0009")
    assert not any(row["key"] == "sms" or row["key"].startswith("sms_tenant:") for row in _rows(engine, "sys_setting"))


def test_reenable_after_upgrade_resets_cutoff_and_does_not_rebroadcast_old_alarm(pre_sms, monkeypatch):
    from sqlalchemy.orm import Session

    cfg, engine = pre_sms
    command.upgrade(cfg, "0010")
    tenant, user, alarm, _ = _outbox_records(engine)
    with engine.begin() as connection:
        connection.execute(AlarmEvent.__table__.update().where(AlarmEvent.id == alarm).values(
            first_seen=NOW - timedelta(minutes=20)))
        contact = connection.execute(Contact.__table__.insert().values(user_id=user, name="电话人员", email="",
            phone="13800000001", enabled=True)).inserted_primary_key[0]
        connection.execute(ContactGroup.__table__.insert().values(code="cutoff", name="合成组",
            member_ids=[contact], leader_ids=[contact]))
        connection.execute(NotifyRoute.__table__.insert().values(name="合成路由", priority=1, match={}, group_code="cutoff",
            escalation=[{"after_min": 15, "to": "leader"}]))
        connection.execute(SysSetting.__table__.insert().values(key="sms", value={"enabled": True,
            "enabled_since": (NOW - timedelta(hours=1)).isoformat()}))
        connection.execute(SysSetting.__table__.insert().values(key=f"sms_tenant:{tenant}", value={
            "enabled": True, "enabled_since": (NOW - timedelta(hours=1)).isoformat(), "revision": 1,
            "endpoint": "https://sms.example.invalid/HttpSmsBatchSend", "username": "synthetic-account",
            "password_cipher": encrypt("synthetic-gateway-secret")}))
    command.downgrade(cfg, "0009")
    command.upgrade(cfg, "0010")
    with Session(engine) as db:
        assert not sms_settings.load_global(db).enabled
        assert sms_service.enqueue_due(db, NOW) == 0
        monkeypatch.setattr(sms_settings, "utcnow", lambda: NOW + timedelta(minutes=1))
        sms_settings.save_global(db, True, "synthetic-admin")
        db.commit()
        assert sms_settings.load_global(db).enabled_since > NOW
        assert sms_settings.effective(db, tenant)
        assert sms_service.enqueue_due(db, NOW + timedelta(minutes=2)) == 0
    assert _rows(engine, "sms_notification") == []


@pytest.mark.pg
def test_pg_contact_sequence_advances_after_explicit_backfill_ids(pre_sms):
    cfg, engine = pre_sms
    contact = Table("contact", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        old = _user(connection, "old", phone=None)
        connection.execute(contact.insert().values(id=9000, user_id=old, name="原联系人", email="", enabled=False))
        _user(connection, "phone-only")
    command.upgrade(cfg, "0010")
    maximum = max(row["id"] for row in _rows(engine, "contact"))
    with engine.begin() as connection:
        later = _user(connection, "later", phone=None)
        identifier = connection.execute(contact.insert().values(user_id=later, name="后续", email="", enabled=False)
                                        .returning(contact.c.id)).scalar_one()
    assert identifier > maximum
