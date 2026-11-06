"""用户 XLSX 导入导出；预览只读，确认参与调用方事务，不提交。

账号是不可变匹配键。空单元格保留原值，ALL / NONE 显式表达全部 / 空租户范围。
初始密码只保存在不可序列化的执行计划中，不进入预览、错误或审计数据。
"""

import io
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser
from app.core.response import BizError, ErrorCode
from app.core.xlsx import new_workbook, to_bytes, write_table
from app.models import HcsAccount, Role, User
from app.schemas.auth import UserCreateIn, UserPatchIn
from app.services import identity

HEADERS = ["账号", "姓名", "邮箱", "电话", "启用", "允许登录", "接收通知", "角色代码", "租户代码", "初始密码"]
WIDTHS = [24, 20, 32, 22, 10, 14, 14, 22, 36, 24]
MAX_ROWS = 5000
MAX_BYTES = 10 * 1024 * 1024
_MAIN_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_FIELDS = {"username": "账号", "display_name": "姓名", "email": "邮箱", "phone": "电话", "enabled": "启用",
           "login_enabled": "允许登录", "notify_enabled": "接收通知", "role_id": "角色代码", "tenant_ids": "租户代码"}


@dataclass
class ImportResult:
    total: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[dict] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"total": self.total, "created": self.created, "updated": self.updated, "unchanged": self.unchanged,
                "error_count": len(self.errors), "errors": self.errors, "items": self.items}


@dataclass
class ImportRow:
    row: int
    username: str
    user_id: int | None
    body: UserCreateIn | UserPatchIn = field(repr=False)


@dataclass
class ImportTodo:
    rows: list[ImportRow] = field(default_factory=list, repr=False)
    errors: list[dict] = field(default_factory=list)


def _bad(message: str) -> BizError:
    return BizError(ErrorCode.BAD_REQUEST, message)


def _manager(actor: CurrentUser) -> None:
    if actor.tenant_ids is not None or not actor.can("user:manage"):
        raise BizError(ErrorCode.FORBIDDEN, "人员导入需要全租户用户管理权限", 403)


def _codes(db: Session) -> tuple[dict[int, str], dict[int, str]]:
    return dict(db.execute(select(Role.id, Role.code)).all()), dict(db.execute(select(HcsAccount.id, HcsAccount.code)).all())


def _scope(ids: list[int] | None, tenants: dict[int, str]) -> str:
    if ids is None:
        return "ALL"
    return ";".join(sorted(tenants.get(i, str(i)) for i in ids)) if ids else "NONE"


def template(db: Session) -> bytes:
    """空数据页以及代码参考；参考页不隐藏或筛掉停用租户。"""
    with db.no_autoflush:
        wb = new_workbook()
        write_table(wb.create_sheet("用户"), HEADERS, [], WIDTHS)
        help_rows = [
            ["按账号匹配，账号不可修改；新人员必须填写账号和姓名。"],
            ["已有人员：空白保留；邮箱、电话、角色代码填写 - 表示清空；登录人员不能清空角色。"],
            ["启用、允许登录、接收通知填写 是/否，也支持 true/false 或 1/0。"],
            ["新人员默认启用、接收通知，但不开通登录；不会生成默认密码。"],
            ["新登录用户须明确填写角色代码、租户代码及初始密码（8–64 字符）。"],
            ["初始密码仅用于新增或首次开通未设置密码的人员；不能重置已有登录密码。"],
            ["租户代码用英文分号分隔；ALL 表示全部，NONE 表示空范围；已有人员留空保留范围。"],
            ["新人员租户代码留空为 NONE；登录人员不能使用 NONE。角色及租户必须填写代码而非名称。"],
            ["最多 5000 行、10 MB，只接受真实 XLSX；禁止公式。先预览，整批校验通过后才写入。"],
        ]
        write_table(wb.create_sheet("填写说明"), ["说明"], help_rows, [110])
        write_table(wb.create_sheet("角色代码"), ["角色代码", "角色名称"],
                    db.execute(select(Role.code, Role.name).order_by(Role.code)), [24, 32])
        write_table(wb.create_sheet("租户代码"), ["租户代码", "租户名称", "启用"],
                    db.execute(select(HcsAccount.code, HcsAccount.name, HcsAccount.enabled).order_by(HcsAccount.code)),
                    [24, 36, 12])
        return to_bytes(wb)


def export_users(db: Session) -> bytes:
    """密码列为空，空范围显式导出为 NONE，字符串使用 core.xlsx 的公式安全写入。"""
    with db.no_autoflush:
        roles, tenants = _codes(db)
        rows = [[u.username, u.display_name, u.email, u.phone, u.enabled, u.login_enabled, u.notify_enabled,
                 roles.get(u.role_id, str(u.role_id)) if u.role_id is not None else None,
                 _scope(u.tenant_ids, tenants), None] for u in db.scalars(select(User).order_by(User.id))]
        wb = new_workbook()
        write_table(wb.create_sheet("用户"), HEADERS, rows, WIDTHS)
        return to_bytes(wb)


def _check_file(content: bytes) -> None:
    if len(content) > MAX_BYTES:
        raise _bad("文件不能超过 10 MB")
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
            if not any(node.get("ContentType") == _MAIN_TYPE for node in types):
                raise _bad("请上传真实的 XLSX 文件")
            # 检查所有工作表，包括不导入的参考页、隐藏页及数据范围外的公式。
            for name in archive.namelist():
                if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                    with archive.open(name) as sheet:
                        for _, node in ElementTree.iterparse(sheet, events=("end",)):
                            if node.tag == "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f":
                                raise _bad("不允许公式单元格，请粘贴为纯文本或数值")
                            node.clear()
    except (BadZipFile, KeyError, ValueError, ElementTree.ParseError, OSError, RuntimeError, NotImplementedError):
        raise _bad("请上传真实且未损坏的 XLSX 文件") from None


def _text(value: Any) -> str | None:
    return str(value).strip() or None if value is not None else None


def _boolean(value: str, label: str) -> bool:
    if value.lower() in {"是", "true", "yes", "y", "1"}:
        return True
    if value.lower() in {"否", "false", "no", "n", "0"}:
        return False
    raise _bad(f"{label}只能填写是/否、true/false 或 1/0")


def _body(values: dict[str, Any], user: User | None, roles: dict[str, int],
          tenants: dict[str, int]) -> UserCreateIn | UserPatchIn:
    data: dict[str, Any] = {}
    for key in ("display_name", "email", "phone"):
        value = _text(values[_FIELDS[key]])
        if value is not None:
            data[key] = None if key != "display_name" and value == "-" else value
    for key in ("enabled", "login_enabled", "notify_enabled"):
        value = _text(values[_FIELDS[key]])
        if value is not None:
            data[key] = _boolean(value, _FIELDS[key])
    role = _text(values["角色代码"])
    if role is not None:
        if role != "-" and role not in roles:
            raise _bad("角色代码不存在，请使用角色的不可变代码")
        data["role_id"] = None if role == "-" else roles[role]
    scope = _text(values["租户代码"])
    if scope is not None:
        if scope.upper() == "ALL":
            data["all_tenants" if user is not None else "tenant_ids"] = True if user is not None else None
        elif scope.upper() == "NONE":
            data["tenant_ids"] = []
        else:
            codes = [code.strip() for code in scope.split(";")]
            if any(code not in tenants for code in codes) or len(set(codes)) != len(codes):
                raise _bad("租户代码不存在或重复；多个租户使用英文分号分隔，ALL/NONE 必须单独使用")
            data["tenant_ids"] = sorted(tenants[code] for code in codes)
    password = values["初始密码"]
    if password is not None and password != "":
        if not isinstance(password, str):
            raise _bad("初始密码必须为文本（8–64 字符）")
        if user is not None and (user.login_enabled or user.password_hash != "!"
                                 or not data.get("login_enabled", user.login_enabled)):
            raise _bad("初始密码仅用于首次开通登录，不能通过导入重置已有密码")
        data["password"] = password
    if user is not None:
        return UserPatchIn.model_validate(data)
    data["username"] = _text(values["账号"])
    data.setdefault("login_enabled", False)
    data.setdefault("tenant_ids", [])
    if data["login_enabled"] and scope is None:
        raise _bad("新登录用户必须显式填写租户代码，或填写 ALL")
    return UserCreateIn.model_validate(data)


def _validation_message(error: ValidationError) -> str:
    # 不使用 str(error) 或 input/ctx：模型级错误可能包含整行初始密码。
    labels = {_FIELDS.get(str(e["loc"][0]), "初始密码" if e["loc"][0] == "password" else "字段")
              for e in error.errors(include_input=False, include_context=False) if e["loc"]}
    return f"{'、'.join(sorted(labels))}格式或长度不正确" if labels else "开通登录须分配角色并设置初始密码"


def _values(body: UserCreateIn | UserPatchIn) -> dict[str, Any]:
    values = body.model_dump(exclude_unset=isinstance(body, UserPatchIn), exclude={"password", "all_tenants"})
    if isinstance(body, UserPatchIn) and body.all_tenants:
        values["tenant_ids"] = None
    return values


def _changes(row: ImportRow, user: User | None, roles: dict[int, str], tenants: dict[int, str]) -> dict:
    changes = {}
    for key, value in _values(row.body).items():
        old = getattr(user, key) if user is not None else None
        if key == "tenant_ids":
            old = _scope(old, tenants) if user is not None else None
            value = _scope(value, tenants)
        elif key == "role_id":
            old = roles.get(old, str(old)) if old is not None else None
            value = roles.get(value, str(value)) if value is not None else None
        if user is None or old != value:
            changes[_FIELDS[key]] = [old, value]
    return changes


def _global_admin(user: User | None, body: UserCreateIn | UserPatchIn | None, admin_id: int | None) -> bool:
    values = _values(body) if body is not None else {}
    return (admin_id is not None and values.get("role_id", user.role_id if user is not None else None) == admin_id
            and values.get("enabled", user.enabled if user is not None else True)
            and values.get("login_enabled", user.login_enabled if user is not None else False)
            and values.get("tenant_ids", user.tenant_ids if user is not None else []) is None)


def _validate(db: Session, rows: list[ImportRow], actor: CurrentUser, users: dict[int, User],
              admin_id: int | None) -> dict[int, str]:
    """只读计算整批最终状态，避免两条单独合法的补丁同时移除最后两名管理员。"""
    errors: dict[int, str] = {}
    patches = {row.user_id: row.body for row in rows if row.user_id is not None}
    before = {u.id for u in users.values() if _global_admin(u, None, admin_id)}
    after = {u.id for u in users.values() if _global_admin(u, patches.get(u.id), admin_id)}
    promoted = any(_global_admin(users.get(row.user_id) if row.user_id is not None else None, row.body, admin_id)
                   for row in rows)
    safe_admin = bool(after) or any(_global_admin(None, row.body, admin_id) for row in rows if row.user_id is None)
    emails: dict[str, int] = {}
    for row in rows:
        user = users.get(row.user_id) if row.user_id is not None else None
        try:
            if row.user_id is None:
                if not isinstance(row.body, UserCreateIn):
                    raise _bad("导入计划类型错误，请重新预览")
                identity.validate_user_create(db, row.body, actor)
            else:
                if user is None or user.username.casefold() != row.username.casefold():
                    raise _bad("原账号已变化或不存在，请重新预览")
                if not isinstance(row.body, UserPatchIn):
                    raise _bad("导入计划类型错误，请重新预览")
                # 登录关闭后保留的旧密码也不允许通过导入重置。
                if row.body.password is not None and user.password_hash != "!":
                    raise _bad("初始密码仅用于首次开通登录，不能通过导入重置已有密码")
                try:
                    identity.validate_user_patch(db, user, row.body, actor)
                except BizError as exc:
                    # 新管理员先写入，允许在同一批内安全交接最后一名管理员。
                    if not (safe_admin and promoted and exc.code == ErrorCode.CONFLICT and "最后一位" in exc.message):
                        raise
            email = _values(row.body).get("email", user.email if user is not None else None)
            if email:
                key = email.casefold()
                if key in emails:
                    errors[emails[key]] = "导入中邮箱重复"
                    raise _bad("导入中邮箱重复")
                emails[key] = row.row
            if before and not safe_admin and row.user_id in before:
                raise BizError(ErrorCode.CONFLICT, "整批导入不能移除最后一位全租户平台管理员", 409)
        except BizError as exc:
            errors[row.row] = exc.message
    return errors


def parse(db: Session, content: bytes, actor: CurrentUser) -> tuple[ImportResult, ImportTodo]:
    """返回安全预览和仅供 apply 使用的执行计划；任何错误都使整份计划不可执行。"""
    _manager(actor)
    _check_file(content)
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
    except Exception:
        raise _bad("请上传真实且未损坏的 XLSX 文件") from None
    try:
        if not wb.worksheets:
            raise _bad("XLSX 缺少用户工作表")
        ws = wb.worksheets[0]
        ws.reset_dimensions()
        cells = ws.iter_rows(values_only=True)
        header = [_text(v) for v in next(cells, ())]
        while header and header[-1] is None:
            header.pop()
        names = [name for name in header if name is not None]
        if len(set(names)) != len(names):
            raise _bad("表头重复，请使用用户导入模板")
        if len(header) != len(HEADERS) or set(names) != set(HEADERS):
            raise _bad("表头缺失或不正确，请保留全部用户导入表头")
        records: list[tuple[int, dict[str, Any], str]] = []
        for number, values in enumerate(cells, 2):
            if not any(_text(v) is not None for v in values):
                continue
            if len(records) == MAX_ROWS:
                raise _bad("单次最多导入 5000 行")
            record = {name: values[index] if index < len(values) else None for index, name in enumerate(names)}
            if any(_text(v) is not None for v in values[len(names):]):
                record["__extra__"] = True
            records.append((number, record, _text(record.get("账号")) or ""))
        counts = Counter(username.casefold() for _, _, username in records if username)
        result, todo = ImportResult(total=len(records)), ImportTodo()
        errors: dict[int, str] = {}
        with db.no_autoflush:
            roles, tenants = _codes(db)
            users = {user.id: user for user in db.scalars(select(User))}
            by_username = {user.username.casefold(): user for user in users.values()}
            for number, values, username in records:
                try:
                    if not username:
                        raise _bad("账号不能为空")
                    if counts[username.casefold()] != 1:
                        raise _bad("导入中账号重复（不区分大小写）")
                    if values.get("__extra__"):
                        raise _bad("存在没有表头的数据列")
                    user = by_username.get(username.casefold())
                    body = _body(values, user, {v: k for k, v in roles.items()}, {v: k for k, v in tenants.items()})
                    todo.rows.append(ImportRow(number, username, user.id if user is not None else None, body))
                except ValidationError as exc:
                    errors[number] = _validation_message(exc)
                except BizError as exc:
                    errors[number] = exc.message
            admin_id = next((i for i, code in roles.items() if code == "admin"), None)
            errors.update(_validate(db, todo.rows, actor, users, admin_id))
            rows = {row.row: row for row in todo.rows}
            for number, _, username in records:
                if number in errors:
                    result.errors.append({"row": number, "username": username, "message": errors[number]})
                    continue
                row = rows[number]
                changes = _changes(row, users.get(row.user_id) if row.user_id is not None else None, roles, tenants)
                action = "create" if row.user_id is None else "update" if changes else "unchanged"
                if action == "create":
                    result.created += 1
                elif action == "update":
                    result.updated += 1
                else:
                    result.unchanged += 1
                result.items.append({"row": number, "username": username, "action": action, "changes": changes})
        todo.errors = result.errors.copy()
        return result, todo
    finally:
        wb.close()


def apply(db: Session, todo: ImportTodo, actor: CurrentUser) -> int:
    """重新验证整批，再在保存点内写入；失败回滚本批，事务由调用方提交。"""
    _manager(actor)
    if todo.errors:
        raise _bad("存在校验错误，请修改后重新导入")
    with db.no_autoflush:
        # 管理员角色锁与 identity 中的逐行保护使用同一把锁。
        admin_id = db.scalar(select(Role.id).where(Role.code == "admin").with_for_update())
        users = {user.id: user for user in db.scalars(select(User).execution_options(populate_existing=True))}
        errors = _validate(db, todo.rows, actor, users, admin_id)
        if errors:
            raise BizError(ErrorCode.BAD_REQUEST, "导入内容已失效，请重新预览", data={"errors": [
                {"row": row.row, "username": row.username, "message": errors[row.row]}
                for row in todo.rows if row.row in errors]})
        roles, tenants = _codes(db)
        ordered = sorted(todo.rows, key=lambda row: not _global_admin(
            users.get(row.user_id) if row.user_id is not None else None, row.body, admin_id))
        applied = 0
        # sqlite3 的延迟 BEGIN 会使最外层 SAVEPOINT 的 RELEASE 意外提交。
        # 显式启动物理事务，确保调用方 rollback 也能撤销已成功的导入。
        connection = db.connection()
        driver = connection.connection.driver_connection
        if connection.dialect.name == "sqlite" and driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN")
        with db.begin_nested():
            for row in ordered:
                user = users.get(row.user_id) if row.user_id is not None else None
                if row.user_id is None:
                    assert isinstance(row.body, UserCreateIn)
                    identity.create_user_record(db, row.body, actor)
                elif user is not None and _changes(row, user, roles, tenants):
                    assert isinstance(row.body, UserPatchIn)
                    identity.update_user_record(db, user, row.body, actor)
                else:
                    continue
                applied += 1
            db.flush()
        return applied
