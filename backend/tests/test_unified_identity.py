"""统一人员入口：账号能力与联系人投影、负责人身份、通知投递一致。"""
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.v1 import auth, notify, users
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user, load_user
from app.core.response import BizError
from app.core.security import create_token, hash_password
from app.main import app as production_app
from app.models import AlarmEvent, Contact, ContactGroup, HcsAccount, Notification, NotifyRoute, Resource, Role, User
from app.models.base import utcnow
from app.notify.dispatcher import dispatch_pending
from app.notify.escalation import escalation_plan
from app.notify.routing import resolve_recipients
from app.schemas.auth import UserCreateIn, UserPatchIn
from app.services import identity


@pytest.fixture()
def directory(mem_db):
    role = Role(code="admin", name="管理员", permissions=["*"])
    viewer = Role(code="viewer", name="访客", permissions=[])
    mem_db.add_all([role, viewer])
    mem_db.flush()
    root = User(username="root-directory", display_name="管理员", role_id=role.id,
                password_hash=hash_password("synthetic-root-password"))
    mem_db.add(root)
    mem_db.add_all([HcsAccount(id=i, code=f"dir-{i}", name=f"范围{i}", iam_endpoint="https://example.invalid",
                               domain_name="synthetic", secret_cipher="") for i in (1, 2)])
    mem_db.flush()
    actor = CurrentUser(id=root.id, username=root.username, display_name=root.display_name,
                        role_code="admin", permissions=["user:manage", "notify:view", "notify:manage", "ledger:view"])
    app = FastAPI()
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: actor
    for router in (users.router, auth.router, notify.router):
        app.include_router(router, prefix="/api/v1")
    with TestClient(app) as client:
        yield mem_db, client, actor, viewer


def _person(db, actor, **fields):
    return identity.create_user_record(db, UserCreateIn(
        username="directory-person", display_name="人员", email="person@example.invalid",
        phone="01234567890", login_enabled=False, **fields), actor)


def _alarm(db, **fields):
    row = AlarmEvent(fingerprint="directory-notification", rule_id="R-test", resource_name="synthetic",
                     service="ecs", resource_type="ecs_server", tenant_id=1, severity="P2", status="FIRING",
                     title="测试", check_item="test", first_seen=utcnow() - timedelta(minutes=30),
                     last_seen=utcnow(), **fields)
    db.add(row)
    db.flush()
    return row


def test_directory_user_created_once_with_contact_and_no_login(directory):
    db, client, actor, _ = directory
    response = client.post("/api/v1/system/users", json={
        "username": "contact-user", "display_name": "联系人", "email": "contact@example.invalid",
        "phone": "01234567890", "login_enabled": False, "tenant_ids": [1],
    })
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["login_enabled"] is False and data["contact_id"]
    contact = db.get(Contact, data["contact_id"])
    assert contact.user_id == data["id"] and contact.name == data["display_name"] and contact.enabled
    assert db.get(User, data["id"]).password_hash == "!"
    assert client.post("/api/v1/auth/login", json={"username": "contact-user", "password": "!"}).status_code == 401
    assert client.post("/api/v1/notify/contacts", json={"name": "绕行", "email": "extra@example.invalid"}).status_code == 409


def test_user_profile_clearing_and_name_changes_sync_contacts_and_active_owners(directory):
    db, client, actor, _ = directory
    person = _person(db, actor, tenant_ids=[1])
    contact = db.scalar(select(Contact).where(Contact.user_id == person.id))
    db.add(ContactGroup(code="identity", name="原组", member_ids=[contact.id], leader_ids=[]))
    resource = Resource(service="ecs", resource_type="ecs_server", resource_id="dir-r", name="资源",
                        tenant_id=1, owner_user_id=person.id, owner=person.display_name)
    db.add(resource)
    db.flush()
    active = _alarm(db, resource_ref=resource.id, owner=person.display_name)
    closed = AlarmEvent(fingerprint="closed-dir", rule_id="R-test", resource_ref=resource.id,
                        tenant_id=1, service="ecs", resource_type="ecs_server", resource_name="资源",
                        severity="P2", status="CLOSED", title="历史", check_item="test", owner=person.display_name)
    db.add(closed)
    db.commit()
    response = client.patch(f"/api/v1/system/users/{person.id}", json={"display_name": "新姓名", "email": None, "phone": None})
    assert response.status_code == 200, response.text
    db.refresh(contact)
    db.refresh(resource)
    db.refresh(active)
    assert contact.id == response.json()["data"]["contact_id"] and contact.name == "新姓名"
    assert contact.email == "" and not contact.enabled and contact.phone is None
    assert resource.owner == active.owner == "新姓名" and closed.owner == "人员"
    assert db.scalar(select(ContactGroup)).member_ids == [contact.id]


def test_first_login_activation_needs_role_password_and_scope(directory):
    db, client, actor, viewer = directory
    person = _person(db, actor, tenant_ids=[1])
    db.commit()
    assert client.patch(f"/api/v1/system/users/{person.id}", json={"login_enabled": True}).status_code == 400
    result = client.patch(f"/api/v1/system/users/{person.id}", json={
        "login_enabled": True, "role_id": viewer.id, "password": "synthetic-first-password"})
    assert result.status_code == 200, result.text
    login = client.post("/api/v1/auth/login", json={"username": person.username, "password": "synthetic-first-password"})
    assert login.status_code == 200
    assert "password" not in result.json()["data"] and "password_hash" not in result.json()["data"]
    assert "synthetic-first-password" not in result.text
    token = login.json()["data"]["access_token"]
    assert load_user(db, token).id == person.id
    assert client.patch(f"/api/v1/system/users/{person.id}", json={"login_enabled": False}).status_code == 200
    with pytest.raises(BizError):
        load_user(db, token)
    assert db.scalar(select(Contact).where(Contact.user_id == person.id)).enabled


def test_directory_false_cannot_reuse_old_token_even_after_reenable(directory):
    db, _, actor, viewer = directory
    person = _person(db, actor, tenant_ids=[1], role_id=viewer.id, password="synthetic-existing")
    token = create_token(person.username, person.id, version=person.token_version)
    with pytest.raises(BizError):
        load_user(db, token)
    identity.update_user_record(db, person, UserPatchIn(login_enabled=True), actor)
    with pytest.raises(BizError):
        load_user(db, token)


@pytest.mark.parametrize("field", ["enabled", "notify_enabled"])
def test_person_disabled_blocks_routing_escalation_and_pending_delivery(directory, field):
    db, _, actor, _ = directory
    person = _person(db, actor, tenant_ids=[1])
    contact = db.scalar(select(Contact).where(Contact.user_id == person.id))
    db.add(ContactGroup(code="dir-notify", name="组", member_ids=[contact.id], leader_ids=[contact.id]))
    db.add(NotifyRoute(name="路由", priority=1, group_code="dir-notify", match={},
                       escalation=[{"to": "leader", "after_min": 15}]))
    alarm = _alarm(db)
    db.flush()
    assert resolve_recipients(db, alarm)[0] == [person.email]
    assert escalation_plan(db, alarm)[0]["emails"] == [person.email]
    n = Notification(alarm_id=alarm.id, kind="FIRING", recipients=[person.email])
    db.add(n)
    identity.update_user_record(db, person, UserPatchIn(**{field: False}), actor)
    db.commit()
    assert resolve_recipients(db, alarm)[0] == [] and escalation_plan(db, alarm) == []
    sent = []
    assert dispatch_pending(db, sender=sent.append) == 0 and sent == []
    assert n.status == "FAILED"


def test_tenant_restricted_person_does_not_receive_other_tenant(directory):
    db, _, actor, _ = directory
    person = _person(db, actor, tenant_ids=[2])
    contact = db.scalar(select(Contact).where(Contact.user_id == person.id))
    db.add(ContactGroup(code="scoped", name="范围", member_ids=[contact.id], leader_ids=[]))
    db.add(NotifyRoute(name="路由", group_code="scoped", match={}))
    alarm = _alarm(db)
    db.flush()
    assert resolve_recipients(db, alarm)[0] == []
    assert identity.user_options(db, [1]) == [item for item in identity.user_options(db, [1]) if item["id"] != person.id]


def test_changed_email_cancels_pending_old_address(directory):
    db, _, actor, _ = directory
    person = _person(db, actor)
    alarm = _alarm(db)
    notice = Notification(alarm_id=alarm.id, kind="FIRING", recipients=[person.email])
    db.add(notice)
    db.flush()
    identity.update_user_record(db, person, UserPatchIn(email="new@example.invalid"), actor)
    db.flush()
    assert notice.status == "MERGED" and notice.recipients == []
    assert db.scalar(select(Contact).where(Contact.user_id == person.id)).email == "new@example.invalid"


def test_secret_validation_does_not_echo_password(directory):
    _, client, _, _ = directory
    response = client.post("/api/v1/system/users", json={"username": "secret-validation", "display_name": "姓名",
                                                        "password": "p@Sx7", "login_enabled": True})
    assert response.status_code == 400 and "p@Sx7" not in response.text


def test_user_options_respects_scope_and_contains_no_account_credentials(directory):
    db, client, actor, _ = directory
    person = _person(db, actor, tenant_ids=[2])
    db.commit()
    actor.tenant_ids = [1]
    response = client.get("/api/v1/system/user-options")
    assert response.status_code == 200
    assert person.id not in {item["id"] for item in response.json()["data"]}
    assert "password_hash" not in response.text and "role_id" not in response.text and "last_login" not in response.text
    assert client.get("/api/v1/system/user-options?tenant_id=2").status_code == 404


def test_generic_settings_cannot_create_another_owner_directory(directory):
    db, _, actor, _ = directory
    from app.models import SysSetting
    from app.services import settings_service

    person = _person(db, actor, tenant_ids=[1])
    db.add(SysSetting(key="ledger_dict", value={"owners": [{"name": "仅历史字典"}], "business": []}))
    db.commit()
    assert [i.name for i in settings_service.get_ledger_dict(db).owners] == sorted(["人员", "管理员"])
    with pytest.raises(BizError, match="用户管理"):
        settings_service.save(db, "ledger_dict", {"owners": [{"name": "另造身份"}]}, "test")
    value, _ = settings_service.save(db, "ledger_dict", {"business": [{"name": "业务"}]}, "test")
    assert person.display_name in [i.name for i in value.owners]
    assert db.get(SysSetting, "ledger_dict").value["owners"] == [{"name": "仅历史字典"}]


def test_smtp_diagnostic_can_target_registered_person_without_resource_scope(directory, monkeypatch):
    db, client, actor, _ = directory
    from app.services.recipients import validate_user_ids

    person = _person(db, actor, tenant_ids=[])
    db.commit()
    with pytest.raises(BizError):
        validate_user_ids(db, [person.id])  # 含全租户数据的报表仍然拒绝空范围人员。
    sent = []
    monkeypatch.setattr("app.api.v1.notify.send", sent.append)
    response = client.post("/api/v1/notify/test-mail", json={"user_ids": [person.id]})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["sent_to"] == [person.email] and len(sent) == 1
    assert "PROD" not in sent[0]["Subject"]
