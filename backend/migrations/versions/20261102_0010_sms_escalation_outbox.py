"""短信升级 Outbox 与缺失的电话人员联系人投影。

不启用短信、不创建短信配置、不保存凭据；保留已有联系人 ID 和联系组成员引用。
降级会永久删除短信 Outbox 及其发送记录；已有短信总开关强制关闭并保留配置，
必须经管理入口重新启用以重置激活时间，避免重升时历史告警因去重记录丢失再发。
电话人员及联系人保留；降级禁用无邮箱投影，重升按人员偏好恢复其派生启停状态。
"""
import re

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def _phone(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r"[\s-]", "", value)
    if value.startswith("+86"):
        value = value[3:]
    elif len(value) == 13 and value.startswith("86"):
        value = value[2:]
    return value if re.fullmatch(r"1[3-9][0-9]{9}", value) else None


def backfill_contacts(connection):
    metadata = sa.MetaData()
    user = sa.Table("sys_user", metadata, autoload_with=connection)
    contact = sa.Table("contact", metadata, autoload_with=connection)
    next_id = (connection.scalar(sa.select(sa.func.max(contact.c.id))) or 0) + 1
    contacts = {row["user_id"]: row["id"] for row in connection.execute(sa.select(
        contact.c.user_id, contact.c.id)).mappings() if row["user_id"] is not None}
    query = sa.select(user.c.id, user.c.display_name, user.c.email, user.c.phone,
                      user.c.enabled, user.c.notify_enabled).order_by(user.c.id)
    for person in connection.execute(query).mappings().all():
        phone = _phone(person["phone"])
        email = person["email"] or ""
        if person["id"] in contacts:
            if not email:
                connection.execute(contact.update().where(contact.c.id == contacts[person["id"]]).values(
                    enabled=bool(person["enabled"] and person["notify_enabled"] and phone)))
            continue
        if phone is None:
            continue
        connection.execute(contact.insert().values(id=next_id, user_id=person["id"], name=person["display_name"],
            email=email, phone=person["phone"],
            enabled=bool(person["enabled"] and person["notify_enabled"] and (email or phone))))
        next_id += 1
    if connection.dialect.name == "postgresql":
        maximum = connection.scalar(sa.select(sa.func.max(contact.c.id)))
        sequence = connection.scalar(sa.text("SELECT pg_get_serial_sequence('contact', 'id')"))
        if maximum is not None and sequence is not None:
            # sequence 来自数据库的 regclass 标识，不接受配置或请求输入。
            connection.execute(sa.text(
                f"SELECT setval(CAST(:sequence AS regclass), GREATEST(:maximum, last_value), true) "
                f"FROM {sequence} WHERE last_value < :maximum OR (last_value = :maximum AND NOT is_called)"
            ), {"sequence": sequence, "maximum": maximum})


def upgrade():
    op.create_table("sms_notification",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("hcs_account.id", ondelete="CASCADE"), nullable=False),
        sa.Column("alarm_id", sa.Integer(), sa.ForeignKey("alarm_event.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("sys_user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("alarm_generation", sa.Integer(), nullable=False),
        sa.Column("dedup_key", sa.String(64), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("route_id", sa.Integer(), nullable=False),
        sa.Column("after_min", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("phone_mask", sa.String(32), nullable=True),
        sa.Column("gateway_code", sa.String(32), nullable=True),
        sa.Column("gateway_message_id", sa.String(128), nullable=True),
        sa.Column("error", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("uk_sms_notification_dedup", "sms_notification", ["dedup_key"], unique=True)
    op.create_index("idx_sms_notification_status", "sms_notification", ["status", "id"])
    for field in ("tenant_id", "alarm_id", "user_id"):
        op.create_index(f"ix_sms_notification_{field}", "sms_notification", [field])
    backfill_contacts(op.get_bind())


def downgrade():
    setting = sa.Table("sys_setting", sa.MetaData(), autoload_with=op.get_bind())
    global_sms = op.get_bind().execute(sa.select(setting.c.value).where(setting.c.key == "sms")).first()
    if global_sms is not None:
        original = global_sms[0]
        value = {**original, "enabled": False} if isinstance(original, dict) else {"enabled": False, "legacy_value": original}
        op.get_bind().execute(setting.update().where(setting.c.key == "sms").values(value=value))
    contact = sa.table("contact", sa.column("email", sa.String()), sa.column("enabled", sa.Boolean()))
    op.get_bind().execute(contact.update().where(sa.or_(contact.c.email.is_(None),
        sa.func.trim(contact.c.email) == "")).values(enabled=False))
    op.drop_table("sms_notification")
