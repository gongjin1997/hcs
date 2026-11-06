import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class LoginIn(BaseModel):
    username: str
    password: str
    remember: bool = False          # 7 天内免登录


class TenantBrief(BaseModel):
    id: int
    code: str
    name: str
    short_name: str
    color: str
    auth_type: str
    enabled: bool


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    email: str | None = None
    role_id: int | None = None
    role_code: str | None = None
    role_name: str
    permissions: list[str] = []
    tenant_ids: list[int] | None = None        # None = 全部租户
    tenants: list[TenantBrief] = []            # 可访问的租户（顶部租户切换）
    is_admin: bool = False                     # 兼容字段：role_code == admin


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class PasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=64)


class UserAdminOut(BaseModel):
    id: int
    phone: str | None = None
    login_enabled: bool = True
    notify_enabled: bool = True
    has_password: bool = True
    contact_id: int | None = None
    owner_count: int = 0
    groups: list[str] = []
    username: str
    display_name: str
    email: str | None = None
    role_id: int | None = None
    role_code: str | None = None
    role_name: str = "未分配角色"
    tenant_ids: list[int] | None = None
    enabled: bool
    last_login_at: datetime | None = None
    created_at: datetime | None = None
    locked: bool = False


class PersonnelFields(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)
    email: str | None = Field(None, max_length=128)
    phone: str | None = Field(None, max_length=32)

    @field_validator("display_name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("姓名不能为空")
        return value.strip()

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("邮箱格式不正确")
        return value

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class UserCreateIn(PersonnelFields):
    username: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{2,31}$")
    role_id: int | None = None
    tenant_ids: list[int] | None = None
    password: str | None = Field(None, min_length=8, max_length=64, repr=False)
    enabled: bool = True
    login_enabled: bool = True
    notify_enabled: bool = True

    @model_validator(mode="after")
    def _login_fields(self):
        if self.login_enabled and (self.role_id is None or self.password is None):
            raise ValueError("开通登录须分配角色并设置初始密码")
        return self


class UserPatchIn(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=64)
    email: str | None = Field(None, max_length=128)
    phone: str | None = Field(None, max_length=32)
    role_id: int | None = None
    tenant_ids: list[int] | None = None
    all_tenants: bool | None = None
    password: str | None = Field(None, min_length=8, max_length=64, repr=False)
    enabled: bool | None = None
    login_enabled: bool | None = None
    notify_enabled: bool | None = None

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            raise ValueError("姓名不能为空")
        return value.strip()

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return PersonnelFields._email(value)

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return PersonnelFields._phone(value)

    @field_validator("enabled", "login_enabled", "notify_enabled")
    @classmethod
    def _flags(cls, value: bool | None) -> bool:
        if value is None:
            raise ValueError("开关不能为 null")
        return value


class ResetPasswordIn(BaseModel):
    password: str = Field(min_length=8, max_length=64)


class RoleOut(BaseModel):
    id: int
    code: str
    name: str
    description: str = ""
    permissions: list[str]
    builtin: bool
    user_count: int = 0


class RoleIn(BaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    name: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=255)
    permissions: list[str] = []


class RolePatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=32)
    description: str | None = Field(default=None, max_length=255)
    permissions: list[str] | None = None
