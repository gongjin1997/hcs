"""统一人员目录；保留联系人 ID、组成员、旧负责人文字及字典 JSON。

仅使用本修订反射到的 SQLAlchemy 表，不依赖当前 ORM 或运行时服务。
降级保留全部人员并禁用不可登录人员的总开关，避免旧版忽略登录开关。
降级快照只保存将删除的字段及关联；再次升级消费快照，不作为运行时数据源。
"""
import hashlib
import logging
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

SNAPSHOT_KEY = "_user_directory_0008"
SNAPSHOT_FORMAT = "hcs-user-directory-0008"
CONTACT_FIELDS = ("name", "email", "phone", "enabled")
logger = logging.getLogger("alembic.runtime.migration")


def _tables(connection):
    metadata = sa.MetaData()
    return {name: sa.Table(name, metadata, autoload_with=connection) for name in (
        "sys_user", "contact", "resource", "sys_setting",
    )}


def _key(value):
    return value.strip().casefold() if isinstance(value, str) else ""


def _in_scope(user, tenant):
    scope = user["tenant_ids"]
    return scope is None or (tenant is not None and isinstance(scope, list) and tenant in scope)


def _dictionary_names(connection, setting):
    value = connection.scalar(sa.select(setting.c.value).where(setting.c.key == "ledger_dict"))
    owners = value.get("owners", []) if isinstance(value, dict) else []
    if not isinstance(owners, list):
        return
    for position, item in enumerate(owners):
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip() and len(name.strip()) <= 64:
            yield position, name.strip()


def reconcile(connection):
    """幂等回填已具备 0008 列的数据库；调用方负责事务，返回歧义记录供核查。

    邮箱必须在既有用户中唯一且尚未占用；不凭名称合并联系人。
    负责人按账号/显示名的候选并集及实际租户范围唯一匹配；已知但无范围的
    候选同样不另造身份。无邮箱用户的通知默认值不会凭空产生收件人。
    """
    tables = _tables(connection)
    user, contact, resource, setting = (tables[name] for name in ("sys_user", "contact", "resource", "sys_setting"))
    users = {row["id"]: dict(row) for row in connection.execute(sa.select(user).order_by(user.c.id)).mappings()}
    contacts = connection.execute(sa.select(contact).order_by(contact.c.id)).mappings().all()
    used = {row["user_id"] for row in contacts if row["user_id"] is not None}
    usernames = {_key(row["username"]) for row in users.values()}
    emails = {}
    for row in users.values():
        if _key(row["email"]):
            emails.setdefault(_key(row["email"]), []).append(row["id"])
    report = {"created_user_ids": [], "ambiguous_contact_ids": [], "ambiguous_resource_ids": [],
              "out_of_scope_resource_ids": [], "ambiguous_dictionary_positions": []}

    next_uid = max(users, default=0) + 1

    def create_user(base, name, *, email=None, phone=None, enabled=True, notify=False, scope=None):
        nonlocal next_uid
        base = base[:28]
        username = base
        suffix = 1
        while _key(username) in usernames:
            suffix += 1
            username = f"{base[:26]}_{suffix}"
        usernames.add(_key(username))
        now = datetime.now(UTC)
        values = dict(username=username, display_name=name, email=email, phone=phone, enabled=enabled,
                      login_enabled=False, notify_enabled=notify, password_hash="!", role_id=None,
                      tenant_ids=scope, failed_attempts=0, token_version=0, created_at=now, updated_at=now)
        uid, next_uid = next_uid, next_uid + 1
        values["id"] = uid
        connection.execute(user.insert().values(**values))
        users[uid] = values
        report["created_user_ids"].append(uid)
        return values

    for row in contacts:
        linked = users.get(row["user_id"])
        if linked is None:
            candidates = emails.get(_key(row["email"]), [])
            if len(candidates) == 1 and candidates[0] not in used:
                linked = users[candidates[0]]
                # 总开关和通知开关独立：用户被禁用不应丢掉原通知偏好。
                changes = {"notify_enabled": bool(row["enabled"])}
                if linked["phone"] is None and row["phone"] is not None:
                    changes["phone"] = row["phone"]
                connection.execute(user.update().where(user.c.id == linked["id"]).values(**changes))
                linked.update(changes)
            else:
                if candidates:
                    report["ambiguous_contact_ids"].append(row["id"])
                # 联系人过去无租户限制；无角色、无登录仍不能获得数据权限。
                linked = create_user(f'legacy_contact_{row["id"]}', row["name"], email=row["email"],
                                     phone=row["phone"], enabled=bool(row["enabled"]), notify=bool(row["enabled"]), scope=None)
            used.add(linked["id"])
        snapshot = dict(user_id=linked["id"], name=linked["display_name"], email=linked["email"] or "",
                        phone=linked["phone"],
                        enabled=bool(linked["enabled"] and linked["notify_enabled"] and _key(linked["email"])))
        if any(row[key] != value for key, value in snapshot.items()):
            connection.execute(contact.update().where(contact.c.id == row["id"]).values(**snapshot))

    names = {}

    def index_user(row):
        for name in {_key(row["username"]), _key(row["display_name"])} - {""}:
            names.setdefault(name, {})[row["id"]] = row

    for row in users.values():
        index_user(row)
    # 不只查活动资源：已释放资源仍有真实历史归属，不丢失其租户。
    owned = {}
    for row in connection.execute(sa.select(resource).order_by(resource.c.id)).mappings():
        if _key(row["owner"]):
            owned.setdefault(_key(row["owner"]), []).append(row)
    for name, resources in owned.items():
        if name not in names:
            tenants = {row["tenant_id"] for row in resources}
            scope = None if None in tenants else sorted(tenants)
            digest = hashlib.sha256(name.encode()).hexdigest()[:20]
            index_user(create_user(f"legacy_owner_{digest}", resources[0]["owner"].strip(), scope=scope))
        for row in resources:
            if row["owner_user_id"] is not None:
                continue
            candidates = [u for u in names[name].values() if _in_scope(u, row["tenant_id"])]
            if len(candidates) == 1:
                connection.execute(resource.update().where(resource.c.id == row["id"]).values(owner_user_id=candidates[0]["id"]))
            elif candidates:
                report["ambiguous_resource_ids"].append(row["id"])
            else:
                report["out_of_scope_resource_ids"].append(row["id"])
    for position, name in _dictionary_names(connection, setting):
        key = _key(name)
        if key not in names:
            digest = hashlib.sha256(key.encode()).hexdigest()[:20]
            index_user(create_user(f"legacy_owner_{digest}", name, scope=[]))
        elif len(names[key]) > 1:
            report["ambiguous_dictionary_positions"].append(position)
    # 原有账号也进入联系人选项，不能要求管理员逐个重新保存才能加入通知组。
    next_cid = max((row["id"] for row in contacts), default=0) + 1
    for row in users.values():
        if row["id"] not in used and _key(row["email"]):
            connection.execute(contact.insert().values(
                id=next_cid, user_id=row["id"], name=row["display_name"], email=row["email"], phone=row["phone"],
                enabled=bool(row["enabled"] and row["notify_enabled"])))
            next_cid += 1
    if connection.dialect.name == "postgresql":
        for table_name in ("sys_user", "contact"):
            connection.execute(sa.text(
                f"SELECT setval(pg_get_serial_sequence(:table_name, 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table_name}), 1), EXISTS(SELECT 1 FROM {table_name}))"
            ), {"table_name": table_name})
    logger.info("0008 directory reconciliation: created=%d ambiguous_contacts=%d ambiguous_resources=%d "
                "out_of_scope_resources=%d ambiguous_dictionary_entries=%d",
                *(len(report[key]) for key in report))
    return report


def _save_snapshot(connection):
    tables = _tables(connection)
    user, contact, resource, setting = (tables[name] for name in ("sys_user", "contact", "resource", "sys_setting"))
    old = connection.execute(sa.select(setting).where(setting.c.key == SNAPSHOT_KEY)).mappings().first()
    previous = None if old is None else dict(value=old["value"], updated_by=old["updated_by"],
                                           updated_at=old["updated_at"].isoformat())
    snapshot = dict(format=SNAPSHOT_FORMAT, previous=previous,
                    users=[dict(row) for row in connection.execute(sa.select(
                        user.c.id, user.c.username, user.c.phone, user.c.login_enabled, user.c.notify_enabled)).mappings()],
                    contacts=[dict(row) for row in connection.execute(sa.select(contact)).mappings()],
                    resources=[dict(row) for row in connection.execute(sa.select(
                        resource.c.id, resource.c.owner, resource.c.tenant_id, resource.c.owner_user_id)).mappings()])
    values = dict(value=snapshot, updated_by=None, updated_at=datetime.now(UTC))
    if old is None:
        connection.execute(setting.insert().values(key=SNAPSHOT_KEY, **values))
    else:
        connection.execute(setting.update().where(setting.c.key == SNAPSHOT_KEY).values(**values))


def _restore_snapshot(connection):
    tables = _tables(connection)
    user, contact, resource, setting = (tables[name] for name in ("sys_user", "contact", "resource", "sys_setting"))
    snapshot = connection.scalar(sa.select(setting.c.value).where(setting.c.key == SNAPSHOT_KEY))
    if not isinstance(snapshot, dict) or snapshot.get("format") != SNAPSHOT_FORMAT:
        return
    current = {row["id"]: dict(row) for row in connection.execute(sa.select(user)).mappings()}
    restored = set()
    for saved in snapshot["users"]:
        existing = current.get(saved["id"])
        if existing is not None and existing["username"] == saved["username"]:
            connection.execute(user.update().where(user.c.id == saved["id"]).values(
                phone=saved["phone"], login_enabled=saved["login_enabled"], notify_enabled=saved["notify_enabled"]))
            restored.add(saved["id"])
    contacts = {row["id"]: row for row in connection.execute(sa.select(contact)).mappings()}
    for saved in snapshot["contacts"]:
        existing = contacts.get(saved["id"])
        if (existing is not None and saved["user_id"] in restored
                and all(existing[key] == saved[key] for key in CONTACT_FIELDS)):
            connection.execute(contact.update().where(contact.c.id == saved["id"]).values(user_id=saved["user_id"]))
    resources = {row["id"]: row for row in connection.execute(sa.select(resource)).mappings()}
    for saved in snapshot["resources"]:
        existing = resources.get(saved["id"])
        if (existing is not None and saved["owner_user_id"] in restored
                and all(existing[key] == saved[key] for key in ("owner", "tenant_id"))):
            connection.execute(resource.update().where(resource.c.id == saved["id"]).values(owner_user_id=saved["owner_user_id"]))
    previous = snapshot["previous"]
    if previous is None:
        connection.execute(setting.delete().where(setting.c.key == SNAPSHOT_KEY))
    else:
        connection.execute(setting.update().where(setting.c.key == SNAPSHOT_KEY).values(
            value=previous["value"], updated_by=previous["updated_by"], updated_at=datetime.fromisoformat(previous["updated_at"])))


def upgrade() -> None:
    with op.batch_alter_table("sys_user") as batch:
        batch.add_column(sa.Column("phone", sa.String(32), nullable=True))
        batch.add_column(sa.Column("login_enabled", sa.Boolean(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("notify_enabled", sa.Boolean(), nullable=False, server_default="1"))
    with op.batch_alter_table("contact") as batch:
        batch.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_contact_user_id_sys_user", "sys_user", ["user_id"], ["id"], ondelete="RESTRICT")
        batch.create_index("ix_contact_user_id", ["user_id"], unique=True)
    with op.batch_alter_table("resource") as batch:
        batch.add_column(sa.Column("owner_user_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_resource_owner_user_id_sys_user", "sys_user", ["owner_user_id"], ["id"], ondelete="SET NULL")
        batch.create_index("ix_resource_owner_user_id", ["owner_user_id"], unique=False)
    connection = op.get_bind()
    _restore_snapshot(connection)
    reconcile(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _save_snapshot(connection)
    user = _tables(connection)["sys_user"]
    # 旧鉴权只认识 enabled；保留原密码/角色但禁用，并撤销已签发会话。
    # 再升级也不自动开启 enabled：需管理员显式确认，不能覆盖旧版期间的禁用决定。
    connection.execute(user.update().where(user.c.login_enabled.is_(False)).values(
        enabled=False, token_version=user.c.token_version + 1))
    with op.batch_alter_table("resource") as batch:
        batch.drop_index("ix_resource_owner_user_id")
        batch.drop_constraint("fk_resource_owner_user_id_sys_user", type_="foreignkey")
        batch.drop_column("owner_user_id")
    with op.batch_alter_table("contact") as batch:
        batch.drop_index("ix_contact_user_id")
        batch.drop_constraint("fk_contact_user_id_sys_user", type_="foreignkey")
        batch.drop_column("user_id")
    with op.batch_alter_table("sys_user") as batch:
        batch.drop_column("notify_enabled")
        batch.drop_column("login_enabled")
        batch.drop_column("phone")
