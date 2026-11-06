"""租户报表、合并报表和平台告警收件人改用统一人员 ID。

旧邮箱数组原样保留至降级；现有人员的角色、租户范围及启停状态不变。
无法唯一且安全匹配的地址另建无角色、不可登录的接收人员，宁可保留明确
的重复邮箱也不猜测身份。reconcile 返回分类计数，不向迁移日志输出邮箱。
降级按当前启用、通知启用且范围允许的人员最新邮箱重建旧配置；显式 []
不会回退到旧邮箱。新建人员和联系人均保留，0008 仍支持其登录禁用标记。
"""
import hashlib
import json
import logging
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

SETTING_KEYS = ("report", "platform_alarm")
logger = logging.getLogger("alembic.runtime.migration")


def _tables(connection):
    metadata = sa.MetaData()
    return {name: sa.Table(name, metadata, autoload_with=connection) for name in (
        "hcs_account", "sys_user", "contact", "sys_setting",
    )}


def _key(value):
    return value.strip().casefold() if isinstance(value, str) else ""


def _email(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 128 or value.count("@") != 1 or any(c.isspace() for c in value):
        return None
    local, domain = value.split("@", 1)
    return value if local and domain else None


def _in_scope(user, tenant):
    scope = user["tenant_ids"]
    return scope is None or (tenant is not None and isinstance(scope, list) and tenant in scope)


def _username(email, tenant):
    identity = json.dumps([_key(email), tenant], ensure_ascii=True, separators=(",", ":"))
    return "recipient_" + hashlib.sha256(identity.encode()).hexdigest()[:22]


def _generated_name(username, base):
    if username == base:
        return True
    prefix, _, suffix = username.rpartition("_")
    return suffix.isdigit() and prefix == base[:31 - len(suffix)]


def _advance_sequences(connection, *tables):
    """显式 ID 回填后只向前推进序列，不能把预留过的高水位重置到 MAX(id)。"""
    if connection.dialect.name != "postgresql":
        return
    for table in tables:
        maximum = connection.scalar(sa.select(sa.func.max(table.c.id)))
        sequence = connection.scalar(sa.text("SELECT pg_get_serial_sequence(:name, 'id')"), {"name": table.fullname})
        if maximum is not None and sequence is not None:
            # sequence 为 PostgreSQL 返回的已正确引用的 regclass 标识，不接受外部输入。
            connection.execute(sa.text(
                f"SELECT setval(CAST(:sequence AS regclass), GREATEST(:maximum, last_value), true) "
                f"FROM {sequence} WHERE last_value < :maximum OR (last_value = :maximum AND NOT is_called)"
            ), {"sequence": sequence, "maximum": maximum})


def reconcile(connection, *, backfill_accounts=False):
    """使用 0009 表结构幂等补齐；不提交事务，也不读取环境或应用服务。

    backfill_accounts 仅用于刚添加 report_user_ids 列的首次升级。普通重复调用
    不会把管理员明确清空的租户 [] 重新解释为旧邮箱。设置 JSON 已有 ID 键
    （包括 []）时同样保留选择。相同邮箱、范围的迁移人员可跨回滚复用。
    """
    tables = _tables(connection)
    account, user, contact, setting = (tables[name] for name in ("hcs_account", "sys_user", "contact", "sys_setting"))
    users = {row["id"]: dict(row) for row in connection.execute(sa.select(user).order_by(user.c.id)).mappings()}
    contacts = connection.execute(sa.select(contact).order_by(contact.c.id)).mappings().all()
    usernames = {_key(row["username"]) for row in users.values()}
    projected = {row["user_id"] for row in contacts if row["user_id"] is not None}
    by_email = {}
    for row in users.values():
        if _key(row["email"]):
            by_email.setdefault(_key(row["email"]), []).append(row)
    next_uid = max(users, default=0) + 1
    next_cid = max((row["id"] for row in contacts), default=0) + 1
    report = {"created_users": 0, "created_contacts": 0, "created_unknown_users": 0,
              "created_ambiguous_users": 0, "created_out_of_scope_users": 0,
              "invalid_recipients": 0, "updated_accounts": 0, "updated_settings": 0}
    resolved = {}

    def ensure_contact(row):
        nonlocal next_cid
        if row["id"] in projected:
            return
        connection.execute(contact.insert().values(
            id=next_cid, user_id=row["id"], name=row["display_name"], email=row["email"], phone=row["phone"],
            enabled=bool(row["enabled"] and row["notify_enabled"])))
        next_cid += 1
        projected.add(row["id"])
        report["created_contacts"] += 1

    def resolve(email, tenant):
        nonlocal next_uid
        cache_key = (_key(email), tenant)
        if cache_key in resolved:
            return resolved[cache_key]
        matches = by_email.get(_key(email), [])
        eligible = [row for row in matches if _in_scope(row, tenant)]
        base = _username(email, tenant)
        scope = None if tenant is None else [tenant]
        # 歧义邮箱的迁移目录本来就是重复项；重跑/再升级不能因此再造一份。
        generated = [row for row in eligible if _generated_name(row["username"], base)
                     and row["login_enabled"] is False and row["role_id"] is None
                     and row["password_hash"] == "!" and row["tenant_ids"] == scope]
        if len(generated) == 1:
            chosen = generated[0]
        elif len(eligible) == 1:
            chosen = eligible[0]
        else:
            username = base
            suffix = 1
            while _key(username) in usernames:
                suffix += 1
                ending = f"_{suffix}"
                username = base[:32 - len(ending)] + ending
            now = datetime.now(UTC)
            chosen = dict(id=next_uid, username=username, display_name=email.split("@", 1)[0][:64], email=email,
                          phone=None, login_enabled=False, notify_enabled=True, password_hash="!", role_id=None,
                          tenant_ids=scope, enabled=True, failed_attempts=0, token_version=0, created_at=now, updated_at=now)
            connection.execute(user.insert().values(**chosen))
            next_uid += 1
            users[chosen["id"]] = chosen
            usernames.add(_key(username))
            by_email.setdefault(_key(email), []).append(chosen)
            report["created_users"] += 1
            reason = "ambiguous" if len(eligible) > 1 else "out_of_scope" if matches else "unknown"
            report[f"created_{reason}_users"] += 1
        ensure_contact(chosen)
        resolved[cache_key] = chosen["id"]
        return chosen["id"]

    def recipient_ids(values, tenant):
        if not isinstance(values, list):
            report["invalid_recipients"] += 1
            return []
        ids, seen = [], set()
        for value in values:
            email = _email(value)
            if email is None:
                report["invalid_recipients"] += 1
                continue
            if _key(email) in seen:
                continue
            seen.add(_key(email))
            uid = resolve(email, tenant)
            if uid not in ids:
                ids.append(uid)
        return ids

    if backfill_accounts:
        for row in connection.execute(sa.select(account).order_by(account.c.id)).mappings():
            if row["report_user_ids"]:
                continue
            ids = recipient_ids(row["report_mail_to"], row["id"])
            if ids != row["report_user_ids"]:
                connection.execute(account.update().where(account.c.id == row["id"]).values(report_user_ids=ids))
                report["updated_accounts"] += 1
    for row in connection.execute(sa.select(setting).where(setting.c.key.in_(SETTING_KEYS)).order_by(setting.c.key)).mappings():
        value = row["value"]
        if not isinstance(value, dict) or "recipient_user_ids" in value:
            continue
        ids = recipient_ids(value.get("mail_to", []), None)
        connection.execute(setting.update().where(setting.c.key == row["key"]).values(
            value={**value, "recipient_user_ids": ids}))
        report["updated_settings"] += 1
    _advance_sequences(connection, user, contact)
    logger.info("0009 recipient reconciliation: created_users=%d created_contacts=%d unknown=%d ambiguous=%d "
                "out_of_scope=%d invalid=%d accounts=%d settings=%d", *report.values())
    return report


def upgrade() -> None:
    # hcs_project 引用此表；SQLite 必须原生 ADD，不能批量重建被引用的父表。
    op.add_column("hcs_account", sa.Column("report_user_ids", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                                          nullable=False, server_default="[]"))
    reconcile(op.get_bind(), backfill_accounts=True)


def downgrade() -> None:
    connection = op.get_bind()
    tables = _tables(connection)
    account, user, setting = (tables[name] for name in ("hcs_account", "sys_user", "sys_setting"))
    users = {row["id"]: row for row in connection.execute(sa.select(user)).mappings()}

    def emails(ids, tenant):
        out, seen = [], set()
        for uid in ids if isinstance(ids, list) else []:
            row = users.get(uid) if type(uid) is int else None
            if row is None or not row["enabled"] or not row["notify_enabled"] or not _in_scope(row, tenant):
                continue
            email = _email(row["email"])
            if email is not None and _key(email) not in seen:
                seen.add(_key(email))
                out.append(email)
        return out

    for row in connection.execute(sa.select(account).order_by(account.c.id)).mappings():
        connection.execute(account.update().where(account.c.id == row["id"]).values(
            report_mail_to=emails(row["report_user_ids"], row["id"])))
    for row in connection.execute(sa.select(setting).where(setting.c.key.in_(SETTING_KEYS))).mappings():
        value = row["value"]
        if not isinstance(value, dict) or "recipient_user_ids" not in value:
            continue
        restored = {key: item for key, item in value.items() if key != "recipient_user_ids"}
        restored["mail_to"] = emails(value["recipient_user_ids"], None)
        connection.execute(setting.update().where(setting.c.key == row["key"]).values(value=restored))
    # SQLite >= 3.35 支持原生 DROP COLUMN；不 DROP/重建有项目外键引用的父表。
    with op.batch_alter_table("hcs_account", recreate="never") as batch:
        batch.drop_column("report_user_ids")
