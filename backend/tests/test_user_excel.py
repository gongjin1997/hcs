"""用户 XLSX 服务契约；仅使用独立数据库与合成数据，不调用 API 或外部服务。"""

import io
import json
from types import SimpleNamespace
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from openpyxl import Workbook, load_workbook
from sqlalchemy import event, select

from app.core.deps import to_current
from app.core.response import BizError, ErrorCode
from app.core.security import verify_password
from app.core.xlsx import to_bytes
from app.models import AuditLog, Contact, HcsAccount, Role, User
from app.services import identity, user_excel

PASSWORD = "synthetic-only-1234"


@pytest.fixture()
def excel_world(mem_db):
    roles = {code: Role(code=code, name=name, permissions=permissions) for code, name, permissions in (
        ("admin", "平台管理员", ["*"]), ("manager", "人员管理", ["user:manage"]), ("viewer", "只读角色", []))}
    tenants = [HcsAccount(code=f"test-{i}", name=f"合成租户{i}", iam_endpoint="https://example.invalid",
                          domain_name="synthetic", enabled=i == 1) for i in (1, 2)]
    mem_db.add_all([*roles.values(), *tenants])
    mem_db.flush()

    def user(username, role_id=None, scope=None, login=True, **kwargs):
        return User(username=username, display_name=f"合成{username}", role_id=role_id, tenant_ids=scope,
                    login_enabled=login, enabled=True, notify_enabled=True,
                    password_hash="test-unusable-hash" if login else "!", **kwargs)

    manager = user("manager", roles["manager"].id)
    admins = [user(f"admin-{i}", roles["admin"].id) for i in (1, 2)]
    directory = user("directory-user", scope=[], login=False, email="directory@example.test", phone="test-phone")
    active = user("active-user", roles["viewer"].id, [tenants[0].id], email="active@example.test")
    mem_db.add_all([manager, *admins, directory, active])
    mem_db.flush()
    identity.sync_contact(mem_db, directory)
    identity.sync_contact(mem_db, active)
    mem_db.commit()
    return SimpleNamespace(db=mem_db, actor=to_current(mem_db, manager), manager=manager, admins=admins,
                           directory=directory, active=active, roles=roles, tenants=tenants)


def workbook(*rows, headers=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "用户"
    columns = user_excel.HEADERS if headers is None else headers
    ws.append(columns)
    for row in rows:
        ws.append([row.get(name) for name in columns])
    return to_bytes(wb)


def preview(world, *rows):
    return user_excel.parse(world.db, workbook(*rows), world.actor)


def snapshot(db):
    return [dict(row._mapping) for row in db.execute(select(User.__table__).order_by(User.id))], [
        dict(row._mapping) for row in db.execute(select(Contact.__table__).order_by(Contact.id))]


def test_template_has_exact_headers_and_all_code_references(excel_world):
    w = excel_world
    before = snapshot(w.db)
    content = user_excel.template(w.db)
    wb = load_workbook(io.BytesIO(content))
    assert list(next(wb["用户"].values)) == user_excel.HEADERS
    assert wb["用户"].max_row == 1
    assert {row[0] for row in list(wb["角色代码"].values)[1:]} == set(w.roles)
    assert list(wb["租户代码"].values)[1:] == [("test-1", "合成租户1", "是"), ("test-2", "合成租户2", "否")]
    assert snapshot(w.db) == before
    wb.close()
    result, todo = user_excel.parse(w.db, content, w.actor)
    assert result.as_dict() == {"total": 0, "created": 0, "updated": 0, "unchanged": 0,
                                "error_count": 0, "errors": [], "items": []}
    assert user_excel.apply(w.db, todo, w.actor) == 0


def test_export_roundtrips_empty_global_and_disabled_tenant_scopes(excel_world):
    w = excel_world
    w.directory.tenant_ids = []
    w.active.tenant_ids = [w.tenants[1].id, w.tenants[0].id]
    w.active.enabled = False
    w.active.notify_enabled = False
    w.db.commit()
    before = snapshot(w.db)
    content = user_excel.export_users(w.db)
    wb = load_workbook(io.BytesIO(content))
    rows = {r[0]: dict(zip(user_excel.HEADERS, r, strict=True)) for r in list(wb.active.values)[1:]}
    assert rows[w.directory.username]["租户代码"] == "NONE"
    assert rows[w.directory.username]["角色代码"] is None
    assert rows[w.manager.username]["租户代码"] == "ALL"
    assert rows[w.active.username]["租户代码"] == "test-1;test-2"
    assert rows[w.active.username]["启用"] == "否"
    assert all(row["初始密码"] is None for row in rows.values())
    assert "password_hash" not in str(rows)
    result, todo = user_excel.parse(w.db, content, w.actor)
    assert result.as_dict()["error_count"] == 0
    assert result.unchanged == len(rows)
    assert user_excel.apply(w.db, todo, w.actor) == 0
    assert snapshot(w.db) == before
    assert not w.tenants[1].enabled
    wb.close()


def test_export_and_template_escape_formula_like_text(excel_world):
    w = excel_world
    w.directory.display_name = '=HYPERLINK("https://example.invalid")'
    w.directory.phone = "+12345"
    w.roles["viewer"].name = "=1+1"
    w.tenants[0].name = "=2+2"
    w.db.commit()
    for content in (user_excel.export_users(w.db), user_excel.template(w.db)):
        wb = load_workbook(io.BytesIO(content))
        assert all(cell.data_type != "f" for ws in wb for row in ws for cell in row)
        result, _ = user_excel.parse(w.db, content, w.actor)
        assert result.as_dict()["error_count"] == 0
        wb.close()


def test_preview_does_not_write_users_contacts_audit_or_flush_pending_changes(excel_world):
    w = excel_world
    writes = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    w.directory.phone = "pending-test-change"
    w.db.autoflush = True
    event.listen(w.db.bind, "before_cursor_execute", record_sql)
    try:
        result, _ = preview(w, {"账号": "new-directory", "姓名": "新增人员", "邮箱": "new@example.test"},
                            {"账号": w.active.username, "姓名": "拟修改姓名", "邮箱": "changed@example.test"})
        assert (result.created, result.updated, result.as_dict()["error_count"]) == (1, 1, 0)
        assert writes == []
        assert w.directory in w.db.dirty
        assert not w.db.new and not w.db.deleted
        assert w.active.display_name == "合成active-user"
    finally:
        event.remove(w.db.bind, "before_cursor_execute", record_sql)
        w.db.autoflush = False
        w.db.rollback()
    assert list(w.db.scalars(select(AuditLog))) == []


def test_new_directory_defaults_have_no_password_role_or_tenant_grants(excel_world):
    w = excel_world
    result, todo = preview(w, {"账号": "new-person", "姓名": "合成人员", "邮箱": "new@example.test"})
    assert result.created == 1 and result.as_dict()["error_count"] == 0
    assert user_excel.apply(w.db, todo, w.actor) == 1
    user = w.db.scalar(select(User).where(User.username == "new-person"))
    assert user.password_hash == "!" and user.role_id is None and user.tenant_ids == []
    assert not user.login_enabled and user.enabled and user.notify_enabled
    contact = w.db.scalar(select(Contact).where(Contact.user_id == user.id))
    assert contact.name == user.display_name and contact.email == user.email and contact.enabled
    w.db.rollback()
    assert w.db.scalar(select(User).where(User.username == "new-person")) is None
    assert w.db.scalar(select(Contact).where(Contact.email == "new@example.test")) is None


@pytest.mark.parametrize("scope, expected", [("ALL", None), ("test-2;test-1", "both")])
def test_new_login_requires_and_uses_explicit_codes_and_password(excel_world, scope, expected):
    w = excel_world
    result, todo = preview(w, {"账号": "new-login", "姓名": "登录人员", "允许登录": True, "角色代码": "viewer",
                               "租户代码": scope, "初始密码": PASSWORD})
    assert result.created == 1 and not result.errors
    assert PASSWORD not in json.dumps(result.as_dict(), ensure_ascii=False)
    assert PASSWORD not in repr(todo) and PASSWORD not in repr(todo.rows)
    assert "初始密码" not in result.items[0]["changes"]
    assert user_excel.apply(w.db, todo, w.actor) == 1
    user = w.db.scalar(select(User).where(User.username == "new-login"))
    assert user.login_enabled and user.role_id == w.roles["viewer"].id
    assert user.tenant_ids == (None if expected is None else [t.id for t in w.tenants])
    assert verify_password(PASSWORD, user.password_hash)


def test_directory_first_activation_accepts_initial_password(excel_world):
    w = excel_world
    result, todo = preview(w, {"账号": w.directory.username, "允许登录": "是", "角色代码": "viewer",
                               "租户代码": "test-2", "初始密码": PASSWORD})
    assert result.updated == 1 and not result.errors
    assert PASSWORD not in json.dumps(result.as_dict())
    assert user_excel.apply(w.db, todo, w.actor) == 1
    assert w.directory.login_enabled and w.directory.tenant_ids == [w.tenants[1].id]
    assert verify_password(PASSWORD, w.directory.password_hash)
    assert w.directory.token_version == 1


@pytest.mark.parametrize("hashed, login", [(False, True), (True, False), (True, True)])
def test_existing_passwords_cannot_be_reset_by_import(excel_world, hashed, login):
    w = excel_world
    w.directory.login_enabled = login
    w.directory.role_id = w.roles["viewer"].id
    w.directory.tenant_ids = [w.tenants[0].id]
    if hashed:
        w.directory.password_hash = "existing-unusable-test-hash"
    w.db.commit()
    before = snapshot(w.db)
    result, todo = preview(w, {"账号": w.directory.username, "允许登录": "是", "初始密码": PASSWORD})
    assert result.as_dict()["error_count"] == 1
    assert PASSWORD not in json.dumps(result.as_dict())
    with pytest.raises(BizError):
        user_excel.apply(w.db, todo, w.actor)
    assert snapshot(w.db) == before


def test_blank_preserves_and_dash_clears_only_nullable_fields(excel_world):
    w = excel_world
    w.directory.role_id = w.roles["viewer"].id
    w.db.commit()
    before = snapshot(w.db)
    result, todo = preview(w, {"账号": w.directory.username})
    assert result.unchanged == 1 and not result.errors
    assert user_excel.apply(w.db, todo, w.actor) == 0
    assert snapshot(w.db) == before
    result, todo = preview(w, {"账号": w.directory.username, "邮箱": "-", "电话": "-", "角色代码": "-"})
    assert result.updated == 1 and not result.errors
    assert user_excel.apply(w.db, todo, w.actor) == 1
    assert (w.directory.email, w.directory.phone, w.directory.role_id) == (None, None, None)
    assert w.directory.tenant_ids == [] and w.directory.username == "directory-user"
    assert not w.db.scalar(select(Contact).where(Contact.user_id == w.directory.id)).enabled


@pytest.mark.parametrize("scope, expected", [("ALL", None), ("NONE", []), ("test-2", "second")])
def test_explicit_scope_tokens_are_preserved(excel_world, scope, expected):
    w = excel_world
    result, todo = preview(w, {"账号": w.directory.username, "租户代码": scope})
    assert not result.errors
    user_excel.apply(w.db, todo, w.actor)
    assert w.directory.tenant_ids == ([w.tenants[1].id] if expected == "second" else expected)


@pytest.mark.parametrize("fields", [
    {"角色代码": "只读角色"}, {"租户代码": "合成租户1"}, {"租户代码": "unknown"}, {"租户代码": "ALL;test-1"},
    {"租户代码": "test-1;test-1"}, {"邮箱": "not-an-email"}, {"邮箱": "active@example.test"}, {"电话": "x" * 33},
    {"启用": "perhaps"}, {"姓名": "x" * 65},
    {"允许登录": "是", "角色代码": "viewer", "租户代码": "ALL"},
    {"允许登录": "是", "角色代码": "viewer", "初始密码": PASSWORD},
    {"允许登录": "是", "角色代码": "viewer", "租户代码": "NONE", "初始密码": PASSWORD},
    {"允许登录": "是", "角色代码": "viewer", "租户代码": "ALL", "初始密码": "short"},
])
def test_invalid_rows_are_safe_and_cannot_be_partially_applied(excel_world, fields):
    w = excel_world
    before = snapshot(w.db)
    result, todo = preview(w, {"账号": "valid-person", "姓名": "有效行"}, {"账号": "bad-person", "姓名": "错误行", **fields})
    assert result.total == 2 and result.created == 1 and result.as_dict()["error_count"] == 1
    assert result.errors[0]["row"] == 3
    assert result.errors[0]["username"] == "bad-person"
    if fields.get("初始密码"):
        assert fields["初始密码"] not in json.dumps(result.as_dict())
    with pytest.raises(BizError):
        user_excel.apply(w.db, todo, w.actor)
    assert snapshot(w.db) == before


def test_existing_directory_activation_requires_initial_password(excel_world):
    w = excel_world
    result, _ = preview(w, {"账号": w.directory.username, "允许登录": "是", "角色代码": "viewer", "租户代码": "ALL"})
    assert result.as_dict()["error_count"] == 1
    assert "初始密码" in result.errors[0]["message"]


def test_duplicate_and_missing_accounts_and_cross_row_emails(excel_world):
    w = excel_world
    result, _ = preview(w, {"账号": "repeat-person", "姓名": "第一行"}, {"账号": "REPEAT-PERSON", "姓名": "第二行"},
                        {"姓名": "无账号"}, {"账号": "email-first", "姓名": "第一邮箱", "邮箱": "same@example.test"},
                        {"账号": "email-second", "姓名": "第二邮箱", "邮箱": "SAME@example.test"})
    assert result.total == result.as_dict()["error_count"] == 5
    assert {e["row"] for e in result.errors} == {2, 3, 4, 5, 6}


@pytest.mark.parametrize("headers", [user_excel.HEADERS[:-1], [*user_excel.HEADERS[:-1], "账号"],
                                    [*user_excel.HEADERS, "未知列"]])
def test_missing_duplicate_and_unknown_headers_are_rejected(excel_world, headers):
    with pytest.raises(BizError, match="表头"):
        user_excel.parse(excel_world.db, workbook(headers=headers), excel_world.actor)


def test_reordered_headers_and_case_insensitive_match_do_not_rename_username(excel_world):
    w = excel_world
    content = workbook({"账号": w.directory.username.upper(), "姓名": "修改姓名"}, headers=list(reversed(user_excel.HEADERS)))
    result, todo = user_excel.parse(w.db, content, w.actor)
    assert result.updated == 1 and not result.errors
    assert user_excel.apply(w.db, todo, w.actor) == 1
    assert w.directory.username == "directory-user" and w.directory.display_name == "修改姓名"


@pytest.mark.parametrize("location", ["A1", "B2", "J2", "Z2", "hidden"])
def test_formulas_in_any_sheet_or_column_are_rejected_without_echoing_content(excel_world, location):
    wb = load_workbook(io.BytesIO(workbook({"账号": "new-person", "姓名": "合成人员"})))
    target = wb.active
    if location == "hidden":
        target = wb.create_sheet("hidden")
        target.sheet_state = "hidden"
        location = "Z9"
    target[location] = f'="{PASSWORD}"'
    with pytest.raises(BizError, match="公式") as error:
        user_excel.parse(excel_world.db, to_bytes(wb), excel_world.actor)
    assert PASSWORD not in str(error.value)


@pytest.mark.parametrize("content", [b"not-xlsx", b"account,name\ntest,name", b"\xd0\xcf\x11\xe0legacy-xls"])
def test_only_real_xlsx_is_accepted(excel_world, content):
    with pytest.raises(BizError, match="XLSX"):
        user_excel.parse(excel_world.db, content, excel_world.actor)


def test_limits_are_enforced_before_database_writes(excel_world):
    w = excel_world
    before = snapshot(w.db)
    with pytest.raises(BizError, match="10 MB"):
        user_excel.parse(w.db, b"x" * (10 * 1024 * 1024 + 1), w.actor)
    content = workbook(*({"账号": "repeated-person", "姓名": "合成人员"} for _ in range(5001)))
    with pytest.raises(BizError, match="5000"):
        user_excel.parse(w.db, content, w.actor)
    assert snapshot(w.db) == before


def test_five_thousand_rows_are_allowed(excel_world):
    content = workbook(*({"账号": "repeated-person", "姓名": "合成人员"} for _ in range(5000)))
    result, _ = user_excel.parse(excel_world.db, content, excel_world.actor)
    assert result.total == result.as_dict()["error_count"] == 5000


def test_incorrect_sheet_dimension_cannot_hide_rows(excel_world):
    content = workbook({"账号": "first-person", "姓名": "第一行"}, {"账号": "second-person", "姓名": "第二行"})
    out = io.BytesIO()
    with ZipFile(io.BytesIO(content)) as source, ZipFile(out, "w") as target:
        for entry in source.infolist():
            data = source.read(entry.filename)
            if entry.filename == "xl/worksheets/sheet1.xml":
                root = ElementTree.fromstring(data)
                dimension = root.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}dimension")
                assert dimension is not None
                dimension.set("ref", "A1:J1")
                data = ElementTree.tostring(root)
            target.writestr(entry, data)
    result, _ = user_excel.parse(excel_world.db, out.getvalue(), excel_world.actor)
    assert result.total == result.created == 2


@pytest.mark.parametrize("fields", [{"启用": "否"}, {"允许登录": "否"}, {"角色代码": "viewer"}, {"租户代码": "test-1"}])
def test_combined_final_state_cannot_remove_both_remaining_global_admins(excel_world, fields):
    w = excel_world
    before = snapshot(w.db)
    result, todo = preview(w, *({"账号": u.username, **fields} for u in w.admins))
    assert result.as_dict()["error_count"] == 2
    assert all("管理员" in error["message"] for error in result.errors)
    with pytest.raises(BizError):
        user_excel.apply(w.db, todo, w.actor)
    assert snapshot(w.db) == before


def test_safe_admin_handover_creates_replacement_before_demotion(excel_world):
    w = excel_world
    w.admins[1].enabled = False
    w.db.commit()
    result, todo = preview(w, {"账号": w.admins[0].username, "启用": "否"},
                          {"账号": "replacement-admin", "姓名": "替代管理员", "允许登录": "是", "角色代码": "admin",
                           "租户代码": "ALL", "初始密码": PASSWORD})
    assert not result.errors and (result.created, result.updated) == (1, 1)
    assert user_excel.apply(w.db, todo, w.actor) == 2
    assert not w.admins[0].enabled
    assert w.db.scalar(select(User).where(User.username == "replacement-admin")).enabled


@pytest.mark.parametrize("change", ["role", "tenant", "email", "username", "last-admin"])
def test_confirm_revalidates_whole_plan_when_database_changes(excel_world, change):
    w = excel_world
    row = {"账号": "new-person", "姓名": "待导入", "邮箱": "new@example.test", "角色代码": "viewer", "租户代码": "test-2"}
    rows = [row, {"账号": w.admins[0].username, "启用": "否"}] if change == "last-admin" else [row]
    result, todo = preview(w, *rows)
    assert not result.errors
    if change == "role":
        w.db.delete(w.roles["viewer"])
    elif change == "tenant":
        w.db.delete(w.tenants[1])
    elif change in {"email", "username"}:
        w.db.add(User(username="new-person" if change == "username" else "concurrent-person", display_name="并发人员",
                      email="new@example.test" if change == "email" else None, password_hash="!", login_enabled=False,
                      tenant_ids=[]))
    else:
        w.admins[1].enabled = False
    w.db.commit()
    before = snapshot(w.db)
    with pytest.raises(BizError):
        user_excel.apply(w.db, todo, w.actor)
    assert snapshot(w.db) == before


@pytest.mark.parametrize("restricted", ["scope", "permission"])
def test_preview_and_confirm_revalidate_manager_authority(excel_world, restricted):
    w = excel_world
    _, todo = preview(w, {"账号": "new-person", "姓名": "合成人员"})
    actor = w.actor.model_copy(update={"tenant_ids": []} if restricted == "scope" else {"permissions": []})
    for operation in (lambda: user_excel.parse(w.db, workbook(), actor), lambda: user_excel.apply(w.db, todo, actor)):
        with pytest.raises(BizError) as error:
            operation()
        assert error.value.code == ErrorCode.FORBIDDEN


def test_import_does_not_bypass_self_protection(excel_world):
    result, _ = preview(excel_world, {"账号": excel_world.actor.username, "启用": "否"})
    assert result.as_dict()["error_count"] == 1 and "自己" in result.errors[0]["message"]


def test_late_failure_rolls_back_users_and_contact_projections(excel_world, monkeypatch):
    w = excel_world
    _, todo = preview(w, {"账号": "first-person", "姓名": "第一人员", "邮箱": "first@example.test"},
                      {"账号": "second-person", "姓名": "第二人员", "邮箱": "second@example.test"})
    original = identity.create_user_record

    def fail_second(db, body, actor):
        if body.username == "second-person":
            raise BizError(ErrorCode.CONFLICT, "合成并发写入失败", 409)
        return original(db, body, actor)

    monkeypatch.setattr(identity, "create_user_record", fail_second)
    before = snapshot(w.db)
    with pytest.raises(BizError, match="合成并发"):
        user_excel.apply(w.db, todo, w.actor)
    # 调用方即使提交，失败批次也不得留下第一条记录或联系人投影。
    w.db.commit()
    assert snapshot(w.db) == before
