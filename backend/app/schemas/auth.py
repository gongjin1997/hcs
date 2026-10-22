from datetime import datetime

from pydantic import BaseModel, Field


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


class UserCreateIn(BaseModel):
    username: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{2,31}$")
    display_name: str = Field(min_length=1, max_length=32)
    email: str | None = None
    role_id: int
    tenant_ids: list[int] | None = None        # 不传 / null = 全部租户
    password: str = Field(min_length=8, max_length=64)


class UserPatchIn(BaseModel):
    display_name: str | None = None
    email: str | None = None
    role_id: int | None = None
    tenant_ids: list[int] | None = None
    all_tenants: bool | None = None            # True：改为“全部租户”（tenant_ids 置空）
    enabled: bool | None = None


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
