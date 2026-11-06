"""ManageOne 租户独立配置；密码仅允许写入，TLS 沿用全局 HCS 校验配置。"""
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

ENDPOINT_ERROR = "ManageOne 地址须为完整的 HTTP/HTTPS 基础地址，不得包含账号、查询参数、片段、模板、空白或控制字符"


class ManageOneConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(False, strict=True)
    endpoint: str = Field("", max_length=512)
    username: str = Field("", max_length=128)
    organization: str | None = Field(None, max_length=128)
    password: str | None = Field(None, max_length=512, repr=False)
    clear_password: bool = Field(False, strict=True)
    select_source: bool = Field(False, strict=True)

    @field_validator("endpoint")
    @classmethod
    def safe_endpoint(cls, value: str) -> str:
        if not value:
            return value
        decoded = unquote(value)
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in decoded):
            raise ValueError(ENDPOINT_ERROR)
        if any(char in decoded for char in "{}\\?#"):
            raise ValueError(ENDPOINT_ERROR)
        try:
            parsed = urlsplit(value)
            valid = parsed.scheme in ("http", "https") and bool(parsed.hostname)
            valid = valid and parsed.username is None and parsed.password is None
            valid = valid and (parsed.port is None or parsed.port > 0) and not parsed.netloc.endswith(":")
            # 拒绝编码后的 userinfo；保留原地址，不猜测局点主机、路径或端口。
            valid = valid and "@" not in unquote(parsed.netloc)
            HttpUrl(value)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(ENDPOINT_ERROR)
        return value

    @field_validator("username", "organization")
    @classmethod
    def safe_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("ManageOne 用户名和组织不能包含控制字符")
        return value.strip()


class ManageOneConfigOut(BaseModel):
    tenant_id: int
    enabled: bool
    configured: bool
    endpoint: str
    username: str
    organization: str | None
    secret_set: bool
    selected: bool
