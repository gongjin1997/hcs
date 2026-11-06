"""账号/租户管理边界回归：仅独立测试库，不创建外部客户端。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.v1 import auth, system, tenants, users
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.core.permissions import PERMISSION_CODES
from app.main import app as production_app
from app.models import HcsAccount, HcsProject, Resource, Role, SysSetting, User
from app.services import settings_service


@pytest.fixture()
def management_api(mem_db):
    admin_role = Role(code="admin", name="管理员", builtin=True, permissions=["*"])
    manager_role = Role(code="manager", name="管理人员", permissions=["user:manage", "role:manage"])
    viewer_role = Role(code="viewer", name="只读", permissions=[])
    mem_db.add_all([admin_role, manager_role, viewer_role])
    mem_db.flush()
    root = User(
        username="last-admin",
        display_name="唯一管理员",
        password_hash="unusable-test-hash",
        role_id=admin_role.id,
        tenant_ids=None,
    )
    manager = User(
        username="manager",
        display_name="管理人员",
        password_hash="unusable-test-hash",
        role_id=manager_role.id,
        tenant_ids=None,
    )
    mem_db.add_all([root, manager])
    for tid in (1, 2):
        mem_db.add(
            HcsAccount(
                id=tid,
                code=f"tenant-{tid}",
                name=f"租户{tid}",
                iam_endpoint="https://example.invalid",
                domain_name="test-only",
                secret_cipher="",
            )
        )
    mem_db.flush()
    for pid, tid in ((11, 1), (21, 2)):
        mem_db.add(
            HcsProject(
                id=pid, account_id=tid, region_id="region-test", project_id=f"project-{pid}", external_domain="example.invalid"
            )
        )
    mem_db.commit()
    user = CurrentUser(
        id=manager.id,
        username=manager.username,
        display_name=manager.display_name,
        role_code="manager",
        permissions=list(PERMISSION_CODES),
        tenant_ids=None,
    )
    app = FastAPI()
    for router in (auth.router, users.router, tenants.router, system.router):
        app.include_router(router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        yield client, user, root, viewer_role


@pytest.mark.parametrize(
    "operation", ["list_users", "create_user", "patch_user", "reset_password", "create_role", "patch_role", "delete_role"]
)
def test_scoped_manager_cannot_change_global_identity_configuration(mem_db, management_api, operation):
    client, cu, root, viewer = management_api
    cu.tenant_ids = [1]
    cu.permissions = ["role:manage"] if operation.endswith("role") else ["user:manage"]
    cases = {
        "list_users": ("get", "/system/users", None),
        "create_user": (
            "post",
            "/system/users",
            {"username": "new-user", "display_name": "测试用户", "password": "synthetic-1234", "role_id": viewer.id},
        ),
        "patch_user": ("patch", f"/system/users/{root.id}", {"display_name": "被篡改"}),
        "reset_password": ("post", f"/system/users/{root.id}/reset-password", {"password": "synthetic-1234"}),
        "create_role": ("post", "/system/roles", {"code": "new_role", "name": "新角色", "permissions": []}),
        "patch_role": ("patch", f"/system/roles/{viewer.id}", {"permissions": ["user:manage"]}),
        "delete_role": ("delete", f"/system/roles/{viewer.id}", None),
    }
    method, path, body = cases[operation]
    response = client.request(method, f"/api/v1{path}", json=body)
    assert response.status_code == 403, response.text
    assert root.display_name == "唯一管理员" and root.password_hash == "unusable-test-hash"
    assert mem_db.get(Role, viewer.id) is not None


def test_full_scope_custom_manager_keeps_legal_capabilities_with_selected_tenant(mem_db, management_api):
    client, cu, root, viewer = management_api
    cu.permissions = ["user:manage", "role:manage"]
    assert cu.role_code != "admin" and cu.tenant_ids is None
    headers = {"X-Tenant-Id": "1"}
    assert client.get("/api/v1/system/users", headers=headers).status_code == 200
    assert (
        client.patch(f"/api/v1/system/users/{root.id}", json={"display_name": "管理员新名"}, headers=headers).status_code == 200
    )
    assert (
        client.post("/api/v1/system/roles", json={"code": "custom_full", "name": "全范围自定义"}, headers=headers).status_code
        == 200
    )
    cu.tenant_ids = [1]
    assert client.get("/api/v1/system/roles").status_code == 200  # 角色元数据合法读取仍可用。
    assert client.get("/api/v1/system/permissions").status_code == 200


def test_tenant_scope_reason_is_distinct_from_function_permission_denial(management_api):
    client, cu, _, _ = management_api
    cu.tenant_ids, cu.permissions = [1], ["tenant:view"]
    denied = client.get("/api/v1/system/tenants", headers={"X-Tenant-Id": "2"})
    assert denied.status_code == 403 and denied.json()["code"] == 40301
    assert denied.json()["message"] == "无权访问该租户的数据"
    assert denied.json()["data"] == {"reason": "tenant_scope"}
    cu.permissions = []
    forbidden = client.get("/api/v1/system/tenants", headers={"X-Tenant-Id": "1"})
    assert forbidden.status_code == 403
    assert (forbidden.json().get("data") or {}).get("reason") != "tenant_scope"


@pytest.mark.parametrize("operation", ["disable", "demote"])
def test_last_enabled_admin_cannot_be_removed_by_another_manager(mem_db, management_api, operation):
    client, _, root, viewer = management_api
    before = root.token_version
    payload = {"enabled": False} if operation == "disable" else {"role_id": viewer.id}
    response = client.patch(f"/api/v1/system/users/{root.id}", json=payload)
    assert response.status_code == 409, response.text
    mem_db.refresh(root)
    assert root.enabled and root.role_id != viewer.id and root.token_version == before
    # 停用的管理员不能作为“还有其他管理员”的依据。
    mem_db.add(
        User(
            username="disabled-admin",
            display_name="停用管理员",
            role_id=root.role_id,
            password_hash="unusable-test-hash",
            enabled=False,
        )
    )
    mem_db.commit()
    assert client.patch(f"/api/v1/system/users/{root.id}", json=payload).status_code == 409


def test_last_global_admin_cannot_lose_full_tenant_scope(mem_db, management_api):
    client, _, root, _ = management_api
    mem_db.add(
        User(
            username="scoped-admin",
            display_name="租户管理员",
            password_hash="unusable-test-hash",
            role_id=root.role_id,
            tenant_ids=[1],
            enabled=True,
        )
    )
    mem_db.commit()
    response = client.patch(f"/api/v1/system/users/{root.id}", json={"tenant_ids": [1]})
    assert response.status_code == 409
    assert root.tenant_ids is None


def test_admin_can_be_disabled_when_another_enabled_admin_exists(mem_db, management_api):
    client, _, root, _ = management_api
    mem_db.add(
        User(
            username="backup-admin",
            display_name="备用管理员",
            role_id=root.role_id,
            password_hash="unusable-test-hash",
            enabled=True,
        )
    )
    mem_db.commit()
    response = client.patch(f"/api/v1/system/users/{root.id}", json={"enabled": False})
    assert response.status_code == 200
    assert not root.enabled and root.token_version == 1


def tenant_body(**overrides):
    return {
        "code": "tenant-1",
        "name": "租户1",
        "iam_endpoint": "https://example.invalid",
        "username": "test-user",
        "projects": [{"id": 11, "region_id": "region-test", "project_id": "project-11", "external_domain": "example.invalid"}],
        **overrides,
    }


def test_project_with_resource_history_cannot_change_cloud_identity(mem_db, management_api):
    client = management_api[0]
    mem_db.add(
        Resource(
            tenant_id=1,
            project_ref=11,
            service="dcs",
            resource_type="dcs_instance",
            resource_id="historical-instance",
            name="历史实例",
            lifecycle="DELETED",
        )
    )
    mem_db.commit()
    body = tenant_body()
    body["projects"][0]["project_id"] = "different-project"
    response = client.put("/api/v1/system/tenants/1", json=body)
    assert response.status_code == 409, response.text
    mem_db.refresh(mem_db.get(HcsProject, 11))
    assert mem_db.get(HcsProject, 11).project_id == "project-11"


@pytest.mark.parametrize("problem", ["foreign_id", "duplicate_id", "duplicate_key"])
def test_tenant_project_input_rejects_foreign_or_duplicate_identity(mem_db, management_api, problem):
    client = management_api[0]
    body = tenant_body()
    if problem == "foreign_id":
        body["projects"][0]["id"] = 21
    elif problem == "duplicate_id":
        body["projects"].append({**body["projects"][0], "project_id": "another-project"})
    else:
        body["projects"].append({k: v for k, v in body["projects"][0].items() if k != "id"})
    response = client.put("/api/v1/system/tenants/1", json=body)
    assert response.status_code == 400, response.text
    assert mem_db.get(HcsProject, 21).account_id == 2


def test_scoped_tenant_manager_keeps_own_tenant_visibility_and_management(management_api):
    client, cu, _, _ = management_api
    cu.permissions, cu.tenant_ids = ["tenant:view", "tenant:manage"], [1]
    assert [t["id"] for t in client.get("/api/v1/system/tenants").json()["data"]] == [1]
    own = client.put("/api/v1/system/tenants/1", json=tenant_body(enabled=False))
    assert own.status_code == 200 and own.json()["data"]["enabled"] is False
    # 停用是采集状态，不撤销查看历史接入配置的合法授权。
    assert [t["id"] for t in client.get("/api/v1/system/tenants").json()["data"]] == [1]
    for method, path, body in (
        ("put", "/system/tenants/2", tenant_body()),
        ("delete", "/system/tenants/2", None),
        ("post", "/system/tenants/2/test", None),
    ):
        assert client.request(method, f"/api/v1{path}", json=body).status_code == 404


def test_deleted_tenant_authorization_is_removed_before_id_can_be_reused(mem_db, management_api):
    from sqlalchemy.orm import Session

    from app.core.deps import load_user, scope_of
    from app.core.response import BizError
    from app.core.security import create_token

    client, _, _, viewer = management_api
    limited = User(
        username="limited-user", display_name="限定租户", password_hash="unusable-test-hash", role_id=viewer.id, tenant_ids=[2]
    )
    mem_db.add(limited)
    mem_db.commit()
    old_token = create_token(limited.username, limited.id, version=limited.token_version)
    assert load_user(mem_db, old_token).tenant_ids == [2]
    assert client.delete("/api/v1/system/tenants/2").status_code == 200
    mem_db.refresh(limited)
    assert limited.tenant_ids == []
    # 显式模拟旧 ID 被重用；PostgreSQL 的序列不会跟随夹具中的手写主键前进。
    replacement = HcsAccount(id=2, code="replacement", name="新租户", iam_endpoint="https://example.invalid",
                             domain_name="test-only")
    mem_db.add(replacement)
    mem_db.commit()
    assert replacement.id not in limited.tenant_ids
    # 模拟下一次请求的新 Session：令牌仍可用，但权限范围必须从 DB 即时读取。
    with Session(bind=mem_db.get_bind()) as next_request:
        current = load_user(next_request, old_token)
        assert current.tenant_ids == []
        with pytest.raises(BizError) as denied:
            scope_of(current, replacement.id)
        assert denied.value.data == {"reason": "tenant_scope"}


def test_settings_get_refreshes_existing_identity_map(mem_db):
    from sqlalchemy.orm import Session

    settings_service.save(mem_db, "collect", {"hcs_concurrency": 2}, "first")
    mem_db.commit()
    held = mem_db.get(SysSetting, "collect")
    assert settings_service.get_collect(mem_db).hcs_concurrency == 2
    with Session(bind=mem_db.get_bind()) as other:
        row = other.get(SysSetting, "collect")
        row.value = {**row.value, "hcs_concurrency": 9}
        other.commit()
    assert settings_service.get_collect(mem_db).hcs_concurrency == 9
    assert held.value["hcs_concurrency"] == 9


def test_scope_change_and_role_change_take_effect_for_existing_access_token(mem_db, management_api):
    from app.core.deps import load_user
    from app.core.security import create_token

    _, _, root, viewer = management_api
    token = create_token(root.username, root.id, version=root.token_version)
    assert load_user(mem_db, token).can("user:manage")
    root.role_id, root.tenant_ids = viewer.id, [1]
    mem_db.commit()
    current = load_user(mem_db, token)
    assert current.permissions == [] and current.tenant_ids == [1]
    root.enabled = False
    mem_db.commit()
    from app.core.response import BizError

    with pytest.raises(BizError):
        load_user(mem_db, token)


def test_user_patch_cannot_store_empty_or_overlong_name(management_api):
    client, _, root, _ = management_api
    for name in ("", "   ", "x" * 65):
        response = client.patch(f"/api/v1/system/users/{root.id}", json={"display_name": name})
        assert response.status_code == 400
    assert root.display_name == "唯一管理员"


def test_auth_revoked_session_remains_rejected_after_user_reenable(mem_db, management_api):
    from app.core.deps import load_user
    from app.core.response import BizError
    from app.core.security import create_token

    _, _, root, _ = management_api
    token = create_token(root.username, root.id, version=root.token_version)
    assert load_user(mem_db, token).id == root.id
    root.token_version += 1
    mem_db.commit()
    with pytest.raises(BizError):
        load_user(mem_db, token)
    assert mem_db.scalar(select(User.id).where(User.username == root.username)) == root.id
