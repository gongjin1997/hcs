"""收件人员目录：独立数据库、合成用户和真实邮件/Excel 生成，不连接外部 SMTP。"""

from datetime import date
from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, func, select, update

from app.core.response import BizError
from app.models import Contact, HcsAccount, Role, SysSetting, User
from app.reports import service as report_service
from app.services import platform_watch, recipients, settings_service


@pytest.fixture()
def recipient_world(mem_db):
    tenants = [HcsAccount(code=f"recipient-{i}", name=f"合成租户{i}", iam_endpoint="https://example.invalid",
                          domain_name="synthetic") for i in (1, 2)]
    admin_role = Role(code="admin", name="合成管理员", permissions=["*"])
    mem_db.add_all([*tenants, admin_role])
    mem_db.flush()

    def person(username, scope=None, **fields):
        return User(username=username, display_name=f"合成{username}", email=f"{username}@example.test", tenant_ids=scope,
                    password_hash="!", login_enabled=False, **fields)

    users = {
        "global_person": person("global-person"),
        "tenant_person": person("tenant-person", [tenants[0].id]),
        "other_person": person("other-person", [tenants[1].id]),
        "unassigned": person("unassigned", []),
        "disabled": person("disabled", enabled=False),
        "notify_off": person("notify-off", notify_enabled=False),
        "no_email": person("no-email"),
        "admin": person("global-admin", role_id=admin_role.id),
        "scoped_admin": person("scoped-admin", [tenants[1].id], role_id=admin_role.id),
    }
    users["no_email"].email = None
    mem_db.add_all(users.values())
    mem_db.commit()
    return SimpleNamespace(db=mem_db, tenants=tenants, admin_role=admin_role, **users)


def configured(world, key, value):
    row = world.db.get(SysSetting, key)
    if row is None:
        row = SysSetting(key=key, value=value, updated_by="synthetic")
        world.db.add(row)
    else:
        row.value = value
    world.db.commit()
    return row


def report(world, *, tenant_id=None):
    return report_service.generate(world.db, "DAILY", date(2026, 11, 1), tenant_id=tenant_id)


def counts(db):
    return tuple(db.scalar(select(func.count()).select_from(model)) for model in (User, Contact))


def current_settings(world, key) -> settings_service.ReportSettings | settings_service.PlatformAlarm:
    value = settings_service.get(world.db, key)
    assert isinstance(value, settings_service.ReportSettings | settings_service.PlatformAlarm)
    return value


def save_settings(world, key, data) -> tuple[settings_service.ReportSettings | settings_service.PlatformAlarm, dict]:
    value, changes = settings_service.save(world.db, key, data, "test")
    assert isinstance(value, settings_service.ReportSettings | settings_service.PlatformAlarm)
    return value, changes


def test_directory_only_users_can_receive_and_ids_are_deduplicated(recipient_world):
    w = recipient_world
    assert not w.global_person.login_enabled
    ids = [w.admin.id, w.global_person.id, w.admin.id]
    assert recipients.validate_user_ids(w.db, ids) == ids[:2]
    assert recipients.emails_for_users(w.db, ids) == [w.admin.email, w.global_person.email]


@pytest.mark.parametrize("value", [True, False, 0, -1, "1", 1.0, None, [], {}])
def test_recipient_ids_must_be_positive_integers_without_bool_coercion(recipient_world, value):
    with pytest.raises(BizError, match="正整数"):
        recipients.validate_user_ids(recipient_world.db, [value])


@pytest.mark.parametrize("name", ["tenant_person", "other_person", "unassigned", "disabled", "notify_off", "no_email"])
def test_new_global_selections_require_full_scope_and_delivery_eligibility(recipient_world, name):
    w = recipient_world
    uid = getattr(w, name).id
    with pytest.raises(BizError, match="用户管理"):
        recipients.validate_user_ids(w.db, [uid])
    assert recipients.emails_for_users(w.db, [uid]) == []
    assert recipients.validate_user_ids(w.db, [uid, uid], existing_ids=[uid]) == [uid]


def test_missing_selection_is_rejected_but_old_reference_is_safe_at_runtime(recipient_world):
    w = recipient_world
    with pytest.raises(BizError, match="用户管理"):
        recipients.validate_user_ids(w.db, [999999])
    assert recipients.validate_user_ids(w.db, [999999], existing_ids=[999999]) == [999999]
    assert recipients.emails_for_users(w.db, [999999]) == []


def test_tenant_reports_allow_global_or_matching_scope_only(recipient_world):
    w = recipient_world
    ids = [w.global_person.id, w.tenant_person.id, w.other_person.id, w.unassigned.id]
    assert recipients.validate_user_ids(w.db, ids[:2], tenant_id=w.tenants[0].id) == ids[:2]
    assert recipients.emails_for_users(w.db, ids, tenant_id=w.tenants[0].id) == [
        w.global_person.email, w.tenant_person.email]
    assert recipients.emails_for_users(w.db, ids, tenant_id=w.tenants[1].id) == [
        w.global_person.email, w.other_person.email]
    with pytest.raises(BizError, match="用户管理"):
        recipients.validate_user_ids(w.db, [w.other_person.id], tenant_id=w.tenants[0].id)


def test_runtime_uses_current_database_email_even_with_cached_user(recipient_world):
    w = recipient_world
    old_email = w.global_person.email
    w.db.execute(update(User).where(User.id == w.global_person.id).values(
        email="new-address@example.test", display_name="改名人员").execution_options(synchronize_session=False))
    w.db.commit()
    assert w.global_person.email == old_email
    assert recipients.emails_for_users(w.db, [w.global_person.id]) == ["new-address@example.test"]
    assert recipients.ids_for_emails(w.db, ["NEW-ADDRESS@example.test"]) == [w.global_person.id]
    with pytest.raises(BizError, match="用户管理"):
        recipients.ids_for_emails(w.db, [old_email])


def test_casefold_email_dedup_and_ambiguous_legacy_addresses(recipient_world):
    w = recipient_world
    w.admin.email = w.global_person.email.upper()
    w.db.commit()
    assert recipients.emails_for_users(w.db, [w.global_person.id, w.admin.id]) == [w.global_person.email]
    with pytest.raises(BizError, match="唯一人员"):
        recipients.ids_for_emails(w.db, [w.global_person.email])
    assert recipients.ids_for_emails(w.db, [w.global_person.email, "unknown@example.test"], strict=False) == []


def test_legacy_ambiguity_is_checked_within_requested_scope(recipient_world):
    w = recipient_world
    w.other_person.email = w.tenant_person.email.upper()
    w.db.commit()
    assert recipients.ids_for_emails(w.db, [w.tenant_person.email], tenant_id=w.tenants[0].id) == [w.tenant_person.id]
    assert recipients.ids_for_emails(w.db, [w.tenant_person.email], tenant_id=w.tenants[1].id) == [w.other_person.id]
    assert recipients.ids_for_emails(w.db, [w.tenant_person.email], strict=False) == []


def test_legacy_resolution_never_creates_users_or_contacts(recipient_world):
    w = recipient_world
    before = counts(w.db)
    assert recipients.ids_for_emails(w.db, ["unknown@example.test", w.global_person.email.upper(),
                                           w.global_person.email, w.tenant_person.email], strict=False) == [w.global_person.id]
    for emails in (["unknown@example.test"], [w.tenant_person.email], ["not-an-email"]):
        with pytest.raises(BizError, match="用户管理"):
            recipients.ids_for_emails(w.db, emails)
    assert recipients.ids_for_emails(w.db, [w.disabled.email]) == [w.disabled.id]
    assert recipients.emails_for_users(w.db, [w.disabled.id]) == []
    assert counts(w.db) == before


@pytest.mark.parametrize("key", ["report", "platform_alarm"])
def test_legacy_settings_resolve_only_known_global_users_without_writes(recipient_world, key):
    w = recipient_world
    raw = {"mail_to": [w.global_person.email, w.tenant_person.email, "unknown@example.test"]}
    configured(w, key, raw)
    before = counts(w.db)
    value = current_settings(w, key)
    assert value.recipient_user_ids == [w.global_person.id]
    assert value.mail_to == [w.global_person.email]
    assert w.db.get(SysSetting, key).value == raw
    assert counts(w.db) == before


@pytest.mark.parametrize("key", ["report", "platform_alarm"])
def test_ids_are_authoritative_even_when_empty_or_all_inactive(recipient_world, key):
    w = recipient_world
    for ids in ([], [w.disabled.id], [w.tenant_person.id], [999999]):
        configured(w, key, {"recipient_user_ids": ids, "mail_to": [w.global_person.email]})
        value = current_settings(w, key)
        assert value.recipient_user_ids == ids
        assert value.mail_to == []


def test_report_environment_legacy_default_is_limited_to_directory_users(recipient_world, monkeypatch):
    w = recipient_world
    settings = settings_service.get_settings().model_copy(update={
        "report_mail_to": [w.global_person.email, w.tenant_person.email, "unknown@example.test"]})
    monkeypatch.setattr(settings_service, "get_settings", lambda: settings)
    value = settings_service.get_report(w.db)
    assert value.recipient_user_ids == [w.global_person.id] and value.mail_to == [w.global_person.email]
    configured(w, "report", {"recipient_user_ids": [], "cron": {"DAILY": "invalid"}})
    value = settings_service.get_report(w.db)
    assert value.recipient_user_ids == [] and value.mail_to == []


@pytest.mark.parametrize("key", ["report", "platform_alarm"])
def test_settings_store_ids_and_project_current_names_addresses_and_flags(recipient_world, key):
    w = recipient_world
    value, changes = save_settings(w, key, {"recipient_user_ids": [w.global_person.id, w.global_person.id]})
    w.db.commit()
    assert value.recipient_user_ids == [w.global_person.id]
    assert "recipient_user_ids" in changes
    assert w.db.get(SysSetting, key).value["recipient_user_ids"] == [w.global_person.id]
    w.global_person.display_name, w.global_person.email = "新姓名", "new-address@example.test"
    w.db.commit()
    assert current_settings(w, key).mail_to == ["new-address@example.test"]
    w.global_person.notify_enabled = False
    w.db.commit()
    assert current_settings(w, key).mail_to == []
    value, _ = save_settings(w, key, {"recipient_user_ids": [w.global_person.id]})
    assert value.recipient_user_ids == [w.global_person.id] and value.mail_to == []
    with pytest.raises(BizError, match="用户管理"):
        settings_service.save(w.db, key, {"recipient_user_ids": [w.global_person.id, w.tenant_person.id]}, "test")


@pytest.mark.parametrize("key", ["report", "platform_alarm"])
def test_legacy_save_resolves_known_users_and_does_not_accept_free_addresses(recipient_world, key):
    w = recipient_world
    before = counts(w.db)
    value, _ = save_settings(w, key, {"mail_to": [w.global_person.email.upper()]})
    assert value.recipient_user_ids == [w.global_person.id] and value.mail_to == [w.global_person.email]
    with pytest.raises(BizError, match="用户管理"):
        settings_service.save(w.db, key, {"mail_to": ["arbitrary@example.test"]}, "test")
    value, _ = save_settings(w, key, {"recipient_user_ids": [], "mail_to": ["ignored@example.test"]})
    assert value.recipient_user_ids == [] and value.mail_to == []
    assert counts(w.db) == before


@pytest.mark.parametrize("key", ["report", "platform_alarm"])
def test_boolean_ids_cannot_be_coerced_when_saving_settings(recipient_world, key):
    with pytest.raises(BizError, match="正整数"):
        settings_service.save(recipient_world.db, key, {"recipient_user_ids": [True]}, "test")


def test_settings_get_and_recipient_helpers_never_autoflush(recipient_world):
    w = recipient_world
    configured(w, "report", {"recipient_user_ids": [w.global_person.id]})
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            statements.append(statement)

    w.global_person.display_name = "待保存姓名"
    w.db.autoflush = True
    event.listen(w.db.bind, "before_cursor_execute", record)
    try:
        assert settings_service.get_report(w.db).recipient_user_ids == [w.global_person.id]
        assert recipients.validate_user_ids(w.db, [w.global_person.id]) == [w.global_person.id]
        assert recipients.ids_for_emails(w.db, [w.global_person.email]) == [w.global_person.id]
        assert recipients.emails_for_users(w.db, [w.global_person.id]) == [w.global_person.email]
        assert statements == [] and w.global_person in w.db.dirty
    finally:
        event.remove(w.db.bind, "before_cursor_execute", record)
        w.db.autoflush = False
        w.db.rollback()


def test_settings_owner_readonly_guard_and_history_are_preserved(recipient_world):
    w = recipient_world
    configured(w, "ledger_dict", {"owners": [{"name": "历史负责人"}], "business": []})
    with pytest.raises(BizError, match="用户管理"):
        settings_service.save(w.db, "ledger_dict", {"owners": [{"name": "自行新增负责人"}]}, "test")
    settings_service.save(w.db, "ledger_dict", {"business": [{"name": "合成业务"}]}, "test")
    assert w.db.get(SysSetting, "ledger_dict").value["owners"] == [{"name": "历史负责人"}]


def test_report_defaults_follow_settings_ids_and_current_email(recipient_world):
    w = recipient_world
    rpt = report(w)
    configured(w, "report", {"recipient_user_ids": [w.global_person.id], "mail_to": ["stale@example.test"]})
    assert report_service.default_recipient_user_ids(w.db, rpt) == [w.global_person.id]
    w.global_person.email = "current@example.test"
    w.db.commit()
    assert report_service.default_recipients(w.db, rpt) == ["current@example.test"]
    sent: list[EmailMessage] = []
    assert report_service.send_report(w.db, rpt, sender=sent.append) == ["current@example.test"]
    assert str(sent[0]["Subject"]) == rpt.title and str(sent[0]["To"]) == "current@example.test"
    filename = list(sent[0].iter_attachments())[0].get_filename()
    assert filename is not None and filename.endswith(".xlsx")
    assert rpt.sent_to == ["current@example.test"] and rpt.sent_at is not None


def test_tenant_report_defaults_use_ids_and_ignore_legacy_addresses(recipient_world):
    w = recipient_world
    rpt = report(w, tenant_id=w.tenants[0].id)
    tenant = w.tenants[0]
    tenant.report_user_ids = [w.tenant_person.id, w.global_person.id, w.other_person.id, w.disabled.id]
    tenant.report_mail_to = ["ignored@example.test"]
    w.db.commit()
    assert report_service.default_recipient_user_ids(w.db, rpt) == tenant.report_user_ids
    assert report_service.default_recipients(w.db, rpt) == [w.tenant_person.email, w.global_person.email]


def test_clearing_tenant_recipient_ids_never_revives_registered_legacy_addresses(recipient_world):
    w = recipient_world
    rpt = report(w, tenant_id=w.tenants[0].id)
    tenant = w.tenants[0]
    tenant.report_user_ids = [w.tenant_person.id]
    tenant.report_mail_to = [w.tenant_person.email, w.global_person.email]
    w.db.commit()
    assert report_service.default_recipients(w.db, rpt) == [w.tenant_person.email]
    tenant.report_user_ids = []
    w.db.commit()
    assert tenant.report_mail_to == [w.tenant_person.email, w.global_person.email]
    assert report_service.default_recipient_user_ids(w.db, rpt) == []
    assert report_service.default_recipients(w.db, rpt) == []
    sent: list[EmailMessage] = []
    with pytest.raises(BizError, match="未指定可用收件人"):
        report_service.send_report(w.db, rpt, sender=sent.append)
    assert sent == [] and rpt.sent_to == []


def test_manual_report_send_by_ids_uses_current_eligible_directory_person(recipient_world):
    w = recipient_world
    rpt = report(w, tenant_id=w.tenants[0].id)
    w.tenant_person.email = "latest@example.test"
    w.db.commit()
    sent: list[EmailMessage] = []
    assert report_service.send_report(w.db, rpt, sender=sent.append,
                                      recipient_user_ids=[w.tenant_person.id, w.tenant_person.id]) == ["latest@example.test"]
    assert str(sent[0]["Subject"]) == rpt.title and str(sent[0]["To"]) == "latest@example.test"


@pytest.mark.parametrize("selection", ["ids", "legacy", "both"])
def test_explicit_empty_manual_recipients_never_fall_back(recipient_world, selection):
    w = recipient_world
    rpt = report(w)
    configured(w, "report", {"recipient_user_ids": [w.global_person.id]})
    sent: list[EmailMessage] = []
    kwargs: dict[str, Any] = {"recipient_user_ids": []} if selection == "ids" else {"to": []}
    if selection == "both":
        kwargs = {"recipient_user_ids": [], "to": [w.global_person.email]}
    with pytest.raises(BizError, match="未指定可用收件人"):
        report_service.send_report(w.db, rpt, sender=sent.append, **kwargs)
    assert sent == [] and rpt.sent_to == []


@pytest.mark.parametrize("name", ["disabled", "notify_off", "no_email", "other_person", "unassigned"])
def test_explicit_manual_ids_validate_entire_selection_before_send(recipient_world, name):
    w = recipient_world
    rpt = report(w, tenant_id=w.tenants[0].id)
    sent: list[EmailMessage] = []
    with pytest.raises(BizError, match="用户管理"):
        report_service.send_report(w.db, rpt, sender=sent.append,
                                   recipient_user_ids=[w.global_person.id, getattr(w, name).id])
    assert sent == [] and rpt.sent_to == []


def test_manual_legacy_to_is_known_users_only_and_scope_checked(recipient_world):
    w = recipient_world
    rpt = report(w, tenant_id=w.tenants[0].id)
    sent: list[EmailMessage] = []
    before = counts(w.db)
    assert report_service.send_report(w.db, rpt, [w.tenant_person.email.upper()], sent.append) == [w.tenant_person.email]
    assert len(sent) == 1
    for emails in (["arbitrary@example.test"], [w.other_person.email]):
        with pytest.raises(BizError, match="用户管理"):
            report_service.send_report(w.db, rpt, emails, sent.append)
    assert len(sent) == 1 and counts(w.db) == before


@pytest.mark.parametrize("field, value", [("enabled", False), ("notify_enabled", False), ("tenant_ids", []), ("email", None)])
def test_default_inactive_recipients_error_without_sending(recipient_world, field, value):
    w = recipient_world
    rpt = report(w)
    configured(w, "report", {"recipient_user_ids": [w.global_person.id]})
    setattr(w.global_person, field, value)
    w.db.commit()
    sent: list[EmailMessage] = []
    assert report_service.default_recipients(w.db, rpt) == []
    with pytest.raises(BizError, match="未指定可用收件人"):
        report_service.send_report(w.db, rpt, sender=sent.append)
    assert sent == [] and rpt.sent_to == []


def test_report_send_preserves_delivery_failure_recording(recipient_world):
    w = recipient_world
    rpt = report(w)

    def fail(message):
        raise RuntimeError("synthetic transport failure")

    with pytest.raises(BizError, match="报表邮件发送失败"):
        report_service.send_report(w.db, rpt, sender=fail, recipient_user_ids=[w.global_person.id])
    assert rpt.send_error == "RuntimeError: synthetic transport failure" and rpt.sent_to == []


def test_platform_default_is_current_eligible_full_scope_admin_only(recipient_world):
    w = recipient_world
    assert platform_watch.fallback_recipients(w.db) == [w.admin.email]
    w.admin.email = "new-admin@example.test"
    w.db.commit()
    assert platform_watch.fallback_recipients(w.db) == ["new-admin@example.test"]
    w.admin.notify_enabled = False
    w.db.commit()
    assert platform_watch.fallback_recipients(w.db) == []


@pytest.mark.parametrize("name", ["disabled", "notify_off", "tenant_person", "no_email"])
def test_configured_invalid_platform_ids_never_fall_back_to_admin(recipient_world, name):
    w = recipient_world
    configured(w, "platform_alarm", {"recipient_user_ids": [getattr(w, name).id], "mail_to": [w.admin.email]})
    assert platform_watch.fallback_recipients(w.db) == []


def test_platform_configured_directory_recipient_tracks_changes(recipient_world):
    w = recipient_world
    configured(w, "platform_alarm", {"recipient_user_ids": [w.global_person.id], "mail_to": ["old@example.test"]})
    assert platform_watch.fallback_recipients(w.db) == [w.global_person.email]
    w.global_person.email = "changed@example.test"
    w.db.commit()
    assert platform_watch.fallback_recipients(w.db) == ["changed@example.test"]
    w.global_person.enabled = False
    w.db.commit()
    assert platform_watch.fallback_recipients(w.db) == []


def test_platform_legacy_configuration_resolves_known_users_only(recipient_world):
    w = recipient_world
    configured(w, "platform_alarm", {"mail_to": [w.global_person.email, "unknown@example.test"]})
    assert platform_watch.fallback_recipients(w.db) == [w.global_person.email]
    w.db.delete(w.admin_role)
    configured(w, "platform_alarm", {"recipient_user_ids": []})
    assert platform_watch.fallback_recipients(w.db) == []
