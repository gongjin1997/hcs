"""运行时邮件设置；响应只提供密码是否存在，永不提供密码或掩码。"""
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

MailSecurity = Literal["NONE", "STARTTLS", "SSL"]
EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


class SmtpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field("localhost", min_length=1, max_length=253)
    port: int = Field(1025, ge=1, le=65535)
    security: MailSecurity = "NONE"
    auth: bool = False
    username: str = Field("", max_length=256)
    sender: str = Field("hcs-inspector@example.com", max_length=254)
    reply_to: str = Field("", max_length=254)

    @field_validator("host", "username", "sender", "reply_to")
    @classmethod
    def clean(cls, value: str) -> str:
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("邮件设置不能包含控制字符")
        return value.strip()

    @field_validator("host")
    @classmethod
    def host_name(cls, value: str) -> str:
        if not value or any(c in value for c in "/@?#\\ "):
            raise ValueError("请填写邮件服务器主机名或 IP，不要包含协议或账号")
        return value

    @field_validator("sender", "reply_to")
    @classmethod
    def email(cls, value: str, info) -> str:
        if (value or info.field_name == "sender") and not EMAIL.fullmatch(value):
            raise ValueError("邮箱格式不正确")
        return value


class ImapConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host: str = Field("", max_length=253)
    port: int = Field(993, ge=1, le=65535)
    security: MailSecurity = "SSL"
    username: str = Field("", max_length=256)

    @field_validator("host", "username")
    @classmethod
    def clean(cls, value: str) -> str:
        return SmtpConfig.clean(value)

    @field_validator("host")
    @classmethod
    def host_name(cls, value: str) -> str:
        return SmtpConfig.host_name(value) if value else value


class PasswordUpdate(BaseModel):
    password: SecretStr | None = Field(None, max_length=1024, repr=False)
    clear_password: bool = False

    @field_validator("password")
    @classmethod
    def no_mask(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            raw = value.get_secret_value()
            if (re.search(r"\*{3,}|[•●]{3,}", raw) or re.fullmatch(r"[*•●]+|.{4}[*•●]+.{4}", raw)
                    or raw.lower() in {"[redacted]", "<redacted>"}):
                raise ValueError("请填写新密码；保留原密码请留空，不要提交掩码")
            if "\r" in raw or "\n" in raw or "\x00" in raw:
                raise ValueError("密码不能包含换行或空字符")
        return value


class SmtpUpdate(SmtpConfig, PasswordUpdate):
    pass


class ImapUpdate(ImapConfig, PasswordUpdate):
    pass


class MailSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    smtp: SmtpUpdate | None = None
    imap: ImapUpdate | None = None


class SmtpOut(SmtpConfig):
    password_set: bool


class ImapOut(ImapConfig):
    password_set: bool


class MailSettingsOut(BaseModel):
    smtp: SmtpOut
    imap: ImapOut
    source: Literal["database", "environment"]
    editable: bool
    updated_by: str | None = None
    updated_at: datetime | None = None


class MailConnectionResult(BaseModel):
    protocol: Literal["SMTP", "IMAP"]
    ok: bool = True
    elapsed_ms: int
    message: str
