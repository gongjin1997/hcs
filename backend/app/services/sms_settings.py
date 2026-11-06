"""短信双开关及租户独立凭据；默认关闭，不继承 HCS 认证或 TLS 选项。"""
import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, Field, StrictBool, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import PREFIX, decrypt, encrypt
from app.core.response import BizError, ErrorCode
from app.models import HcsAccount, Role, SysSetting
from app.models.base import utcnow

GLOBAL_KEY = "sms"
SmsSeverity = Literal["P1", "P2", "P3"]


def _default_levels() -> list[SmsSeverity]:
    return ["P1", "P2"]


class SmsGlobalConfig(BaseModel):
    enabled: StrictBool = False
    enabled_since: AwareDatetime | None = None


class SmsTenantConfig(BaseModel):
    enabled: StrictBool = False
    endpoint: str = ""
    username: str = ""
    password_cipher: str = Field(default="", exclude=True, repr=False)
    expand_port: str = ""
    verify_tls: StrictBool = True
    severities: list[SmsSeverity] = Field(default_factory=_default_levels)
    revision: int = 0
    enabled_since: AwareDatetime | None = None


class SmsTenantUpdate(BaseModel):
    enabled: StrictBool = False
    endpoint: str = Field("", max_length=512)
    username: str = Field("", max_length=128)
    password: str | None = Field(None, max_length=512, repr=False)
    clear_password: bool = False
    expand_port: str = Field("", max_length=14)
    verify_tls: StrictBool = True
    severities: list[SmsSeverity] = Field(default_factory=_default_levels, max_length=3)

    @field_validator("endpoint", "username", "expand_port")
    @classmethod
    def _strip(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("短信配置不能包含控制字符")
        return value.strip()

    @field_validator("severities")
    @classmethod
    def _levels(cls, value: list) -> list:
        return list(dict.fromkeys(value))


def tenant_key(tenant_id: int) -> str:
    return f"sms_tenant:{tenant_id}"


def load_global(db: Session) -> SmsGlobalConfig:
    row = db.get(SysSetting, GLOBAL_KEY, populate_existing=True)
    try:
        return SmsGlobalConfig.model_validate(row.value if row and isinstance(row.value, dict) else {})
    except ValueError:
        return SmsGlobalConfig()  # 损坏配置关闭，不能触发短信。


def load_tenant(db: Session, tenant_id: int) -> SmsTenantConfig:
    row = db.get(SysSetting, tenant_key(tenant_id), populate_existing=True)
    try:
        return SmsTenantConfig.model_validate(row.value if row and isinstance(row.value, dict) else {})
    except ValueError:
        return SmsTenantConfig()


def password(config: SmsTenantConfig) -> str:
    if not config.password_cipher.startswith(PREFIX):
        raise BizError(ErrorCode.BAD_REQUEST, "短信凭据不可用，请重新保存该租户密码")
    try:
        return decrypt(config.password_cipher)
    except Exception:
        raise BizError(ErrorCode.BAD_REQUEST, "短信凭据无法解密，请重新保存该租户密码") from None


def public_tenant(config: SmsTenantConfig) -> dict:
    return {**config.model_dump(mode="json"), "password_set": config.password_cipher.startswith(PREFIX)}


def _store(db: Session, key: str, value: dict, operator: str) -> None:
    row = db.get(SysSetting, key)
    if row is None:
        db.add(SysSetting(key=key, value=value, updated_by=operator))
    else:
        row.value, row.updated_by = value, operator
    db.flush()


def save_global(db: Session, enabled: bool, operator: str) -> dict:
    db.scalar(select(Role.id).where(Role.code == "admin").with_for_update())
    before = load_global(db)
    if before.enabled == enabled:
        return {}
    after = SmsGlobalConfig(enabled=enabled, enabled_since=utcnow() if enabled else before.enabled_since)
    _store(db, GLOBAL_KEY, after.model_dump(mode="json"), operator)
    return {"enabled": [before.enabled, enabled]}


def _validate_endpoint(endpoint: str) -> None:
    if not endpoint:
        return
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
        valid = parsed.scheme in ("http", "https") and bool(parsed.hostname) and (port is None or port > 0)
        valid = valid and not any((parsed.username, parsed.password, parsed.query, parsed.fragment))
        valid = valid and not any(char.isspace() for char in endpoint) and "{" not in endpoint and "}" not in endpoint
    except ValueError:
        valid = False
    if not valid:
        raise BizError(ErrorCode.BAD_REQUEST, "请填写完整短信接口 HTTP/HTTPS 地址，不含账号、查询参数或占位符")


def save_tenant(db: Session, tenant_id: int, body: SmsTenantUpdate, operator: str) -> dict:
    db.scalar(select(HcsAccount.id).where(HcsAccount.id == tenant_id).with_for_update())
    before = load_tenant(db, tenant_id)
    updates = body.model_dump(exclude_unset=True, exclude={"password", "clear_password"})
    values = {**before.model_dump(), **updates}
    cipher = before.password_cipher
    if body.password and body.clear_password:
        raise BizError(ErrorCode.BAD_REQUEST, "设置和清空短信密码不能同时选择")
    if body.password:
        if set(body.password) == {"*"}:
            raise BizError(ErrorCode.BAD_REQUEST, "请填写实际短信密码，留空保留原密码")
        cipher = encrypt(body.password)
    elif body.clear_password:
        cipher = ""
    values["password_cipher"] = cipher
    after = SmsTenantConfig.model_validate(values)
    _validate_endpoint(after.endpoint)
    if after.expand_port and (not after.expand_port.isascii() or not after.expand_port.isdigit()):
        raise BizError(ErrorCode.BAD_REQUEST, "扩展端口仅能填写不超过 14 位数字")
    if after.enabled and (not after.endpoint or not after.username or not cipher.startswith(PREFIX) or not after.severities):
        raise BizError(ErrorCode.BAD_REQUEST, "开启租户短信须填写网关、账号、密码并选择告警级别")
    changed = {key: [getattr(before, key), getattr(after, key)] for key in updates
               if getattr(before, key) != getattr(after, key)}
    secret_changed = cipher != before.password_cipher
    if not changed and not secret_changed:
        return {}
    after.revision = before.revision + 1
    if after.enabled and not before.enabled:
        after.enabled_since = utcnow()
    stored = after.model_dump(mode="json")
    stored["password_cipher"] = cipher
    _store(db, tenant_key(tenant_id), stored, operator)
    if secret_changed:
        changed["password_set"] = [bool(before.password_cipher), bool(cipher)]
    return changed


def effective(db: Session, tenant_id: int) -> bool:
    global_config = load_global(db)
    if not global_config.enabled or global_config.enabled_since is None:
        return False
    tenant = db.get(HcsAccount, tenant_id, populate_existing=True)
    config = load_tenant(db, tenant_id)
    return bool(tenant and tenant.enabled and config.enabled and config.enabled_since is not None
                and config.endpoint and config.username
                and config.password_cipher.startswith(PREFIX) and config.severities)


def activation_time(db: Session, tenant_id: int) -> datetime | None:
    times = [load_global(db).enabled_since, load_tenant(db, tenant_id).enabled_since]
    return max(value for value in times if value is not None) if any(times) else None


def normalize_phone(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    phone = re.sub(r"[\s-]", "", value)
    if phone.startswith("+86"):
        phone = phone[3:]
    elif phone.startswith("86") and len(phone) == 13:
        phone = phone[2:]
    return phone if re.fullmatch(r"1[3-9][0-9]{9}", phone) else None
