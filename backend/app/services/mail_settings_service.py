"""全局邮件配置。无进程缓存；首次保存将有效配置（含环境默认值）完整写入 SysSetting。"""
from pydantic import BaseModel, Field, SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.response import BizError, ErrorCode
from app.models import SysSetting
from app.models.base import utcnow
from app.schemas.mail import ImapConfig, MailSettingsUpdate, SmtpConfig

KEY = "mail"


class SmtpRuntime(SmtpConfig):
    password: SecretStr = Field(default_factory=lambda: SecretStr(""), exclude=True, repr=False)
    ca_file: str | None = Field(None, exclude=True, repr=False)


class ImapRuntime(ImapConfig):
    password: SecretStr = Field(default_factory=lambda: SecretStr(""), exclude=True, repr=False)


class MailRuntime(BaseModel):
    smtp: SmtpRuntime
    imap: ImapRuntime


def from_environment(settings: Settings | None = None) -> MailRuntime:
    s = settings or get_settings()
    return MailRuntime(smtp=SmtpRuntime(
        host=s.smtp_host, port=s.smtp_port, security="STARTTLS" if s.smtp_starttls else "NONE",
        auth=bool(s.smtp_user), username=s.smtp_user or "", password=SecretStr(s.smtp_password or ""),
        sender=s.smtp_sender, reply_to=s.smtp_reply_to or "", ca_file=s.smtp_ca_file,
    ), imap=ImapRuntime(host="", port=993, username=""))


def _row(db: Session) -> SysSetting | None:
    # 同一 Session 再次读取也刷新 identity map，避免长期运行 worker 使用旧配置。
    return db.scalar(select(SysSetting).where(SysSetting.key == KEY).execution_options(populate_existing=True))


def get(db: Session | None = None) -> MailRuntime:
    if db is None:
        with SessionLocal() as session:
            return get(session)
    row = _row(db)
    if row is None:
        return from_environment()
    try:
        smtp = dict(row.value["smtp"])
        imap = dict(row.value["imap"])
        smtp_cipher = smtp.pop("password_encrypted", "")
        imap_cipher = imap.pop("password_encrypted", "")
        smtp["password"] = SecretStr(crypto.decrypt(smtp_cipher) if smtp_cipher else "")
        imap["password"] = SecretStr(crypto.decrypt(imap_cipher) if imap_cipher else "")
        # 空密码显式保存在 DB，不能再退回环境密码。
        return MailRuntime(smtp=SmtpRuntime(**smtp), imap=ImapRuntime(**imap))
    except Exception:
        raise BizError(ErrorCode.INTERNAL, "邮件配置读取失败，请检查加密密钥或重新保存邮件设置", 500) from None


def public(db: Session, *, editable: bool) -> dict:
    value = get(db)
    row = _row(db)
    return {"smtp": {**value.smtp.model_dump(), "password_set": bool(value.smtp.password.get_secret_value())},
            "imap": {**value.imap.model_dump(), "password_set": bool(value.imap.password.get_secret_value())},
            "source": "database" if row else "environment", "editable": editable,
            "updated_by": row.updated_by if row else None, "updated_at": row.updated_at if row else None}


def save(db: Session, body: MailSettingsUpdate, operator: str) -> dict:
    """返回仅非 secret 变更；调用方负责审计与提交，密码从不进入 diff。"""
    current = get(db)
    stored: dict = {}
    changes: dict = {}
    for name, model in (("smtp", SmtpConfig), ("imap", ImapConfig)):
        before = getattr(current, name)
        patch = getattr(body, name)
        data = before.model_dump()
        password = before.password.get_secret_value()
        if patch is not None:
            data.update(patch.model_dump(exclude_unset=True, exclude={"password", "clear_password"}))
            new_password = patch.password.get_secret_value() if patch.password is not None else ""
            if patch.clear_password and new_password:
                raise BizError(ErrorCode.BAD_REQUEST, "新密码与清除密码不能同时设置")
            if patch.clear_password:
                password = ""
            elif new_password:
                password = new_password
        try:
            after = model.model_validate(data)
        except ValidationError:
            raise BizError(ErrorCode.BAD_REQUEST, "邮件设置格式不正确，请检查主机、端口和邮箱地址") from None
        if isinstance(after, SmtpConfig) and after.auth and not after.username:
            raise BizError(ErrorCode.BAD_REQUEST, "SMTP 启用认证时必须填写账号")
        if isinstance(after, ImapConfig) and after.enabled and (not after.host or not after.username):
            raise BizError(ErrorCode.BAD_REQUEST, "启用 IMAP 时必须填写服务器与账号")
        after_data = after.model_dump()
        changes.update({f"{name}.{k}": [old, after_data[k]]
                        for k, old in before.model_dump().items() if old != after_data[k]})
        stored[name] = {**after_data, "password_encrypted": crypto.encrypt(password) if password else ""}
        if name == "smtp":
            # 保留已有部署的 CA 路径快照（不读证书，不经 API 展示），保存后不再依赖邮件环境变量。
            stored[name]["ca_file"] = before.ca_file
    row = _row(db)
    if row is None:
        row = SysSetting(key=KEY)
        db.add(row)
    row.value, row.updated_by, row.updated_at = stored, operator, utcnow()
    db.flush()
    return changes
