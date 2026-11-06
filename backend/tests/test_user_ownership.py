"""资源负责人统一引用用户目录：真实独立数据库、台账编辑与 XLSX 往返。"""
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import func, select

from app.api.v1.resources import router
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.ledger import importer
from app.main import app as production_app
from app.models import AlarmEvent, AuditLog, HcsAccount, Resource, ResourceChange, SysSetting, User
from app.models.alarm import ACTIVE_STATUSES
from app.services import settings_service
from app.services.settings_service import LedgerDict


def _user(db, username="owner-account", display_name="资源负责人", **kwargs):
    user = User(username=username, display_name=display_name, password_hash="", login_enabled=False, **kwargs)
    db.add(user)
    db.flush()
    return user


def _resource(db, **kwargs):
    resource = Resource(**{"tenant_id": 101, "service": "vpc", "resource_type": "vpc", "resource_id": "vpc-1",
                           "name": "测试资源", **kwargs})
    db.add(resource)
    db.flush()
    return resource


def _alarms(db, resource):
    alarms = []
    for status in (*ACTIVE_STATUSES, "RESOLVED", "CLOSED"):
        alarm = AlarmEvent(tenant_id=resource.tenant_id, resource_ref=resource.id,
                           fingerprint=f"ownership-{resource.id}-{status}", rule_id="ownership-rule",
                           service=resource.service, resource_type=resource.resource_type,
                           resource_name=resource.name, severity="P2", status=status, title="测试告警",
                           check_item="status", owner=resource.owner)
        db.add(alarm)
        alarms.append(alarm)
    db.flush()
    return alarms


def _xlsx(resource, *, owner=None, account=None, remark=None, business=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "台账管理字段"
    ws.append(["资源 ID", "负责人", "负责人账号", "备注", "业务系统", "台账 ID"])
    ws.append([resource.resource_id, owner, account, remark, business, resource.id])
    stream = io.BytesIO()
    wb.save(stream)
    wb.close()
    return stream.getvalue()


@pytest.fixture()
def ownership_api(mem_db):
    for tid in (101, 202):
        mem_db.add(HcsAccount(id=tid, code=f"ownership-{tid}", name=f"租户{tid}",
                              iam_endpoint="https://example.invalid", domain_name="test-only"))
    mem_db.commit()
    actor = CurrentUser(id=9001, username="ledger-editor", display_name="台账维护员",
                        permissions=["ledger:view", "ledger:edit"], tenant_ids=None)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: actor
    with TestClient(app) as client:
        yield client, actor


@pytest.mark.parametrize("assignment", ["id", "username", "display_name"])
def test_patch_assigns_stable_id_and_display_name(mem_db, ownership_api, assignment):
    client, _ = ownership_api
    user = _user(mem_db, tenant_ids=[101], notify_enabled=False)
    resource = _resource(mem_db, owner="旧负责人")
    mem_db.commit()
    payload = {"owner_user_id": user.id} if assignment == "id" else {"owner": getattr(user, assignment)}
    response = client.patch(f"/api/v1/resources/{resource.id}", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["owner_user_id"] == user.id
    assert response.json()["data"]["owner"] == user.display_name
    mem_db.refresh(resource)
    assert (resource.owner_user_id, resource.owner) == (user.id, user.display_name)
    change = mem_db.scalar(select(ResourceChange).where(ResourceChange.resource_ref == resource.id))
    assert change.source == "MANUAL"
    assert {c["field"] for c in change.changes} == {"owner", "owner_user_id"}
    assert next(c for c in change.changes if c["field"] == "owner")["new"] == user.display_name
    audit = mem_db.scalar(select(AuditLog).where(AuditLog.action == "ledger.update"))
    assert audit.detail["changes"]["owner_user_id"] == [None, user.id]


def test_patch_id_is_authoritative_over_legacy_name(mem_db, ownership_api):
    client, _ = ownership_api
    user = _user(mem_db)
    resource = _resource(mem_db)
    mem_db.commit()
    response = client.patch(f"/api/v1/resources/{resource.id}",
                            json={"owner_user_id": user.id, "owner": "不存在的旧名字"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["owner"] == user.display_name


def test_patch_ambiguous_display_name_requires_username_or_id(mem_db, ownership_api):
    client, _ = ownership_api
    first = _user(mem_db, "first", "同名人员")
    second = _user(mem_db, "second", "同名人员")
    resource = _resource(mem_db)
    mem_db.commit()
    bad = client.patch(f"/api/v1/resources/{resource.id}", json={"owner": first.display_name})
    assert bad.status_code == 400 and "重名" in bad.json()["message"]
    assert resource.owner_user_id is None and resource.owner is None
    good = client.patch(f"/api/v1/resources/{resource.id}", json={"owner": second.username})
    assert good.status_code == 200 and good.json()["data"]["owner_user_id"] == second.id


def test_patch_can_reassign_between_users_with_same_display_name(mem_db, ownership_api):
    client, _ = ownership_api
    first = _user(mem_db, "first", "同名人员")
    second = _user(mem_db, "second", "同名人员")
    resource = _resource(mem_db, owner=first.display_name, owner_user_id=first.id)
    alarms = _alarms(mem_db, resource)
    mem_db.commit()
    response = client.patch(f"/api/v1/resources/{resource.id}", json={"owner_user_id": second.id})
    assert response.status_code == 200 and response.json()["data"]["owner_user_id"] == second.id
    assert response.json()["data"]["owner"] == first.display_name
    change = mem_db.scalar(select(ResourceChange))
    assert change.changes == [{"field": "owner_user_id", "label": "负责人用户 ID", "old": first.id, "new": second.id}]
    for alarm in alarms:
        mem_db.refresh(alarm)
        assert alarm.owner == first.display_name


@pytest.mark.parametrize("kwargs", [{"enabled": False}, {"tenant_ids": [202]}, {"tenant_ids": []}])
@pytest.mark.parametrize("by_id", [True, False])
def test_patch_rejects_disabled_or_ineligible_owner(mem_db, ownership_api, kwargs, by_id):
    client, _ = ownership_api
    user = _user(mem_db, **kwargs)
    resource = _resource(mem_db, owner="历史负责人")
    mem_db.commit()
    payload = {"owner_user_id": user.id} if by_id else {"owner": user.username}
    bad = client.patch(f"/api/v1/resources/{resource.id}", json=payload)
    assert bad.status_code == 400, bad.text
    assert "用户管理" in bad.json()["message"]
    mem_db.refresh(resource)
    assert resource.owner == "历史负责人" and resource.owner_user_id is None
    assert mem_db.scalar(select(func.count()).select_from(ResourceChange)) == 0


def test_patch_unknown_owner_does_not_create_user(mem_db, ownership_api):
    client, _ = ownership_api
    resource = _resource(mem_db)
    mem_db.commit()
    for payload in ({"owner": "未登记人员"}, {"owner_user_id": 99999}):
        bad = client.patch(f"/api/v1/resources/{resource.id}", json=payload)
        assert bad.status_code == 400
    assert mem_db.scalar(select(func.count()).select_from(User)) == 0


def test_global_resource_requires_an_owner_with_global_tenant_scope(mem_db, ownership_api):
    client, _ = ownership_api
    scoped = _user(mem_db, "scoped", "租户人员", tenant_ids=[101])
    global_user = _user(mem_db, "global", "全局人员")
    resource = _resource(mem_db, tenant_id=None)
    mem_db.commit()
    bad = client.patch(f"/api/v1/resources/{resource.id}", json={"owner_user_id": scoped.id})
    assert bad.status_code == 400
    good = client.patch(f"/api/v1/resources/{resource.id}", json={"owner_user_id": global_user.id})
    assert good.status_code == 200 and good.json()["data"]["owner_user_id"] == global_user.id


def test_scoped_editor_cannot_change_resource_in_other_tenant(mem_db, ownership_api):
    client, actor = ownership_api
    user = _user(mem_db)
    resource = _resource(mem_db, tenant_id=202)
    mem_db.commit()
    actor.tenant_ids = [101]
    response = client.patch(f"/api/v1/resources/{resource.id}", json={"owner_user_id": user.id})
    assert response.status_code == 404
    assert resource.owner_user_id is None
    assert mem_db.scalar(select(func.count()).select_from(ResourceChange)) == 0


@pytest.mark.parametrize("payload", [{"remark": "仅改备注"}, {"owner": "未匹配历史名字", "remark": "仅改备注"}])
def test_patch_preserves_unresolved_legacy_owner_for_unrelated_edits(mem_db, ownership_api, payload):
    client, _ = ownership_api
    resource = _resource(mem_db, owner="未匹配历史名字")
    mem_db.commit()
    response = client.patch(f"/api/v1/resources/{resource.id}", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["owner"] == "未匹配历史名字"
    assert response.json()["data"]["owner_user_id"] is None
    assert mem_db.scalar(select(func.count()).select_from(User)) == 0


@pytest.mark.parametrize("payload", [{"owner": None}, {"owner": "  "}, {"owner_user_id": None},
                                    {"owner_user_id": None, "owner": "资源负责人"}])
def test_patch_clear_removes_identity_and_updates_only_active_alarms(mem_db, ownership_api, payload):
    client, _ = ownership_api
    user = _user(mem_db)
    resource = _resource(mem_db, owner=user.display_name, owner_user_id=user.id)
    alarms = _alarms(mem_db, resource)
    mem_db.commit()
    response = client.patch(f"/api/v1/resources/{resource.id}", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["owner"] is None and response.json()["data"]["owner_user_id"] is None
    for alarm in alarms:
        mem_db.refresh(alarm)
        assert alarm.owner == (None if alarm.status in ACTIVE_STATUSES else user.display_name)


def test_patch_owner_updates_active_alarms_by_resource_reference(mem_db, ownership_api):
    client, _ = ownership_api
    user = _user(mem_db)
    resource = _resource(mem_db, owner="历史负责人")
    alarms = _alarms(mem_db, resource)
    other = _resource(mem_db, resource_id="other", tenant_id=202, owner="历史负责人")
    others = _alarms(mem_db, other)
    mem_db.commit()
    response = client.patch(f"/api/v1/resources/{resource.id}", json={"owner_user_id": user.id})
    assert response.status_code == 200, response.text
    for alarm in [*alarms, *others]:
        mem_db.refresh(alarm)
        expected = user.display_name if alarm.resource_ref == resource.id and alarm.status in ACTIVE_STATUSES else "历史负责人"
        assert alarm.owner == expected


def test_owner_options_come_from_eligible_users_and_usage_uses_ids(mem_db, ownership_api):
    client, _ = ownership_api
    first = _user(mem_db, "first", "同名人员", tenant_ids=[101])
    second = _user(mem_db, "second", "同名人员", tenant_ids=[101])
    global_user = _user(mem_db, "global", "全局人员")
    _user(mem_db, "other-tenant", "其他租户人员", tenant_ids=[202])
    _user(mem_db, "disabled", "停用人员", enabled=False)
    no_tenants = _user(mem_db, "no-tenants", "无租户人员", tenant_ids=[])
    _resource(mem_db, owner=first.display_name, owner_user_id=first.id)
    _resource(mem_db, resource_id="legacy", owner=first.display_name)
    _resource(mem_db, resource_id="outside", owner=first.display_name, owner_user_id=first.id, tenant_id=202)
    mem_db.add(SysSetting(key="ledger_dict", value={"owners": [{"name": "旧字典人员"}]}, updated_by="test"))
    mem_db.commit()
    headers = {"X-Tenant-Id": "101"}
    data = client.get("/api/v1/resources/dict", headers=headers).json()["data"]
    owners = {row["id"]: row for row in data["owners"]}
    assert set(owners) == {first.id, second.id, global_user.id}
    assert owners[first.id]["used"] == 1 and owners[second.id]["used"] == 0
    assert owners[second.id]["username"] == second.username
    assert data["owners_outside"] == [{"name": first.display_name, "used": 1}]
    assert all(set(row) == {"id", "username", "name", "remark", "used"} for row in owners.values())
    types = client.get("/api/v1/resources/types", headers=headers).json()["data"]
    assert set(types["dict"]["owners"]) == {first.display_name, global_user.display_name}
    full = client.get("/api/v1/resources/dict").json()["data"]
    assert no_tenants.id not in {row["id"] for row in full["owners"]}


def test_dict_owners_are_read_only_for_scoped_editor(mem_db, ownership_api):
    client, actor = ownership_api
    _user(mem_db)
    mem_db.commit()
    actor.tenant_ids = [101]
    response = client.put("/api/v1/resources/dict", json={"business": [], "enforce": True})
    assert response.status_code == 403


@pytest.mark.parametrize("new_owners", [[], [{"name": "新增目录人员"}], None])
def test_dict_rejects_replacing_user_directory(mem_db, ownership_api, new_owners):
    client, _ = ownership_api
    _user(mem_db)
    mem_db.commit()
    response = client.put("/api/v1/resources/dict", json={"owners": new_owners})
    assert response.status_code == 400 and "用户管理" in response.json()["message"]


@pytest.mark.parametrize("compatibility", ["absent", "full", "legacy_projection"])
def test_dict_put_accepts_derived_compatibility_without_writing_owners(mem_db, ownership_api, compatibility):
    client, _ = ownership_api
    _user(mem_db)
    mem_db.add(SysSetting(key="ledger_dict", value={"owners": [{"name": "持久化历史字典"}]}, updated_by="test"))
    mem_db.commit()
    body = {"business": [{"name": "业务系统"}], "enforce": True}
    if compatibility != "absent":
        owners = client.get("/api/v1/resources/dict").json()["data"]["owners"]
        body["owners"] = owners if compatibility == "full" else [{"name": o["name"], "remark": None} for o in owners]
    response = client.put("/api/v1/resources/dict", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["enforce"] is True
    assert response.json()["data"]["owners"][0]["username"] == "owner-account"
    saved = settings_service.get_ledger_dict(mem_db)
    assert [o.name for o in saved.owners] == ["资源负责人"]
    assert mem_db.get(SysSetting, "ledger_dict").value["owners"] == [{"name": "持久化历史字典"}]
    assert [b.name for b in saved.business] == ["业务系统"]


@pytest.mark.parametrize("field,value", [("name", "新增人员"), ("username", "renamed"), ("id", 9999),
                                       ("remark", "编辑人员备注")])
def test_dict_put_rejects_owner_changes_with_manage_users_message(mem_db, ownership_api, field, value):
    client, _ = ownership_api
    _user(mem_db)
    mem_db.commit()
    body = client.get("/api/v1/resources/dict").json()["data"]
    body["owners"][0][field] = value
    response = client.put("/api/v1/resources/dict", json=body)
    assert response.status_code == 400 and "用户管理" in response.json()["message"]
    assert mem_db.scalar(select(func.count()).select_from(AuditLog)) == 0


def test_business_dictionary_enforcement_remains_independent_of_owners(mem_db, ownership_api):
    client, _ = ownership_api
    user = _user(mem_db)
    resource = _resource(mem_db)
    settings_service.save(mem_db, "ledger_dict", {"business": [{"name": "已登记系统"}], "enforce": True}, "test")
    mem_db.commit()
    bad = client.patch(f"/api/v1/resources/{resource.id}", json={"business": "未知系统"})
    assert bad.status_code == 400 and "台账字典" in bad.json()["message"]
    good = client.patch(f"/api/v1/resources/{resource.id}", json={"owner": user.username, "business": "已登记系统"})
    assert good.status_code == 200, good.text
    result, todo = importer.parse(mem_db, _xlsx(resource, business="未知系统"), None,
                                  settings_service.get_ledger_dict(mem_db))
    assert not todo and "台账字典" in result.errors[0]["message"]


@pytest.mark.parametrize("column", ["name_username", "name_display", "account", "account_ambiguous_display"])
def test_import_assigns_canonical_user_and_display_name_without_owner_dictionary(mem_db, column):
    user = _user(mem_db, tenant_ids=[101])
    resource = _resource(mem_db, owner="历史负责人")
    if column == "account_ambiguous_display":
        _user(mem_db, "same-name", user.display_name)
    mem_db.commit()
    owner = user.username if column == "name_username" else user.display_name
    account = user.username if column.startswith("account") else None
    result, todo = importer.parse(mem_db, _xlsx(resource, owner=owner, account=account), [101],
                                  LedgerDict(enforce=True, owners=[]))
    assert not result.errors and len(todo) == 1
    assert resource.owner == "历史负责人" and resource.owner_user_id is None  # 预览不写入。
    assert mem_db.scalar(select(func.count()).select_from(ResourceChange)) == 0
    assert importer.apply(mem_db, todo, "测试维护员") == 1
    mem_db.commit()
    mem_db.refresh(resource)
    assert (resource.owner_user_id, resource.owner) == (user.id, user.display_name)
    change = mem_db.scalar(select(ResourceChange))
    assert change.source == "IMPORT" and change.operator == "测试维护员"
    assert {c["field"] for c in change.changes} == {"owner", "owner_user_id"}
    assert next(c for c in change.changes if c["field"] == "owner")["new"] == user.display_name


def test_import_account_is_canonical_when_only_account_is_changed(mem_db):
    first = _user(mem_db, "first", "原负责人")
    second = _user(mem_db, "second", "新负责人")
    resource = _resource(mem_db, owner=first.display_name, owner_user_id=first.id)
    mem_db.commit()
    result, todo = importer.parse(mem_db, _xlsx(resource, owner=first.display_name, account=second.username),
                                  None, LedgerDict())
    assert not result.errors and len(todo) == 1
    importer.apply(mem_db, todo, "test")
    assert (resource.owner_user_id, resource.owner) == (second.id, second.display_name)


def test_import_account_column_requires_username_even_for_unique_display_name(mem_db):
    user = _user(mem_db)
    resource = _resource(mem_db)
    mem_db.commit()
    result, todo = importer.parse(mem_db, _xlsx(resource, account=user.display_name), None, LedgerDict())
    assert not todo and "账号" in result.errors[0]["message"]
    assert resource.owner is None and resource.owner_user_id is None


def test_import_api_preview_and_apply_use_same_identity_and_audit(mem_db, ownership_api):
    client, _ = ownership_api
    user = _user(mem_db, tenant_ids=[101])
    resource = _resource(mem_db)
    mem_db.commit()
    content = _xlsx(resource, owner=user.username)
    files = {"file": ("ownership.xlsx", content)}
    preview = client.post("/api/v1/resources/import", files=files)
    assert preview.status_code == 200 and preview.json()["data"]["changed"] == 1
    assert resource.owner is None and resource.owner_user_id is None
    assert mem_db.scalar(select(func.count()).select_from(AuditLog)) == 0
    applied = client.post("/api/v1/resources/import", files=files, params={"dry_run": "false"})
    assert applied.status_code == 200 and applied.json()["data"]["applied"] == 1
    mem_db.refresh(resource)
    assert (resource.owner_user_id, resource.owner) == (user.id, user.display_name)
    audit = mem_db.scalar(select(AuditLog).where(AuditLog.action == "ledger.import"))
    assert audit is not None and audit.detail["changed"] == 1


def test_import_scope_cannot_be_expanded_by_owner_account(mem_db, ownership_api):
    client, actor = ownership_api
    user = _user(mem_db)
    resource = _resource(mem_db, tenant_id=202)
    mem_db.commit()
    actor.tenant_ids = [101]
    files = {"file": ("ownership.xlsx", _xlsx(resource, account=user.username))}
    response = client.post("/api/v1/resources/import", files=files, params={"dry_run": "false"})
    assert response.status_code == 400 and response.json()["data"]["error_count"] == 1
    assert resource.owner_user_id is None


@pytest.mark.parametrize("invalid", ["unknown", "ambiguous", "disabled", "other_tenant", "empty_scope"])
@pytest.mark.parametrize("account_column", [False, True])
def test_import_rejects_invalid_changed_owner_without_creating_users(mem_db, invalid, account_column):
    resource = _resource(mem_db, owner="历史负责人")
    target = "unknown"
    if invalid == "ambiguous":
        user = _user(mem_db, "first", "同名人员")
        _user(mem_db, "second", "同名人员")
        target = user.display_name
    elif invalid != "unknown":
        options = {"disabled": {"enabled": False}, "other_tenant": {"tenant_ids": [202]},
                   "empty_scope": {"tenant_ids": []}}
        user = _user(mem_db, **options[invalid])
        target = user.username
    mem_db.commit()
    count = mem_db.scalar(select(func.count()).select_from(User))
    content = _xlsx(resource, account=target) if account_column else _xlsx(resource, owner=target)
    result, todo = importer.parse(mem_db, content, None, LedgerDict())
    assert len(result.errors) == 1 and not todo
    assert "负责人" in result.errors[0]["message"]
    assert mem_db.scalar(select(func.count()).select_from(User)) == count
    assert resource.owner == "历史负责人" and resource.owner_user_id is None


@pytest.mark.parametrize("legacy", ["unknown", "ambiguous", "disabled", "other_tenant"])
def test_import_preserves_unchanged_invalid_legacy_owner_on_unrelated_edits(mem_db, legacy):
    if legacy == "unknown":
        name = "未登记的历史人员"
    elif legacy == "ambiguous":
        name = _user(mem_db, "first", "同名人员").display_name
        _user(mem_db, "second", name)
    else:
        name = _user(mem_db, **({"enabled": False} if legacy == "disabled" else {"tenant_ids": [202]})).display_name
    resource = _resource(mem_db, owner=name)
    mem_db.commit()
    result, todo = importer.parse(mem_db, _xlsx(resource, owner=name, remark="只改备注"), None, LedgerDict(enforce=True))
    assert not result.errors and len(todo) == 1
    assert todo[0][1] == {"remark": "只改备注"}
    importer.apply(mem_db, todo, "test")
    assert resource.owner == name and resource.owner_user_id is None and resource.remark == "只改备注"


def test_import_template_appends_account_and_roundtrips_existing_assignment(mem_db, ownership_api):
    user = _user(mem_db)
    resource = _resource(mem_db, owner=user.display_name, owner_user_id=user.id)
    mem_db.commit()
    content = importer.template(mem_db, ["vpc"], None)
    wb = load_workbook(io.BytesIO(content))
    ws = wb["台账管理字段"]
    assert [c.value for c in ws[1]] == ["租户", "资源类型", "资源 ID", "名称", "负责人", "业务系统", "环境",
                                       "核心资源", "允许关机", "备注", "台账 ID", "负责人账号"]
    assert ws.cell(2, 11).value == resource.id and ws.cell(2, 12).value == user.username
    wb.close()
    result, todo = importer.parse(mem_db, content, None, LedgerDict())
    assert not result.errors and not todo and result.unchanged == 1


def test_import_existing_disabled_assignment_does_not_block_unrelated_changes(mem_db):
    user = _user(mem_db, enabled=False)
    resource = _resource(mem_db, owner=user.display_name, owner_user_id=user.id)
    mem_db.commit()
    result, todo = importer.parse(mem_db, _xlsx(resource, owner=user.display_name, account=user.username, remark="备注"),
                                  None, LedgerDict())
    assert not result.errors and todo[0][1] == {"remark": "备注"}
    importer.apply(mem_db, todo, "test")
    assert resource.owner_user_id == user.id and resource.owner == user.display_name


@pytest.mark.parametrize("clear_column", [None, "owner", "account"])
def test_import_owner_change_propagates_only_to_active_linked_alarms(mem_db, clear_column):
    first = _user(mem_db, "first", "旧负责人")
    second = _user(mem_db, "second", "新负责人")
    resource = _resource(mem_db, owner=first.display_name, owner_user_id=first.id)
    alarms = _alarms(mem_db, resource)
    other = _resource(mem_db, resource_id="other", owner=first.display_name)
    others = _alarms(mem_db, other)
    mem_db.commit()
    owner = "-" if clear_column == "owner" else first.display_name
    account = "-" if clear_column == "account" else second.username
    result, todo = importer.parse(mem_db, _xlsx(resource, owner=owner, account=account), None, LedgerDict())
    assert not result.errors and len(todo) == 1
    importer.apply(mem_db, todo, "test")
    mem_db.commit()
    expected_name = None if clear_column else second.display_name
    assert resource.owner_user_id == (None if clear_column else second.id)
    for alarm in [*alarms, *others]:
        mem_db.refresh(alarm)
        expected = (expected_name if alarm.resource_ref == resource.id and alarm.status in ACTIVE_STATUSES
                    else first.display_name)
        assert alarm.owner == expected
