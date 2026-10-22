from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.metrics import SOURCES


class ProjectIn(BaseModel):
    id: int | None = None
    region_id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    project_name: str | None = None
    external_domain: str = Field(min_length=1, max_length=128)
    env: str = "PROD"
    workspaces: list[str] = []
    enabled: bool = True


class TenantIn(BaseModel):
    """租户接入：auth_type=PASSWORD 填 IAM 用户名 + 密码；AKSK 填 AK + SK（+ 账号 ID，调用 IAM 时用）。secret 留空 = 不修改。"""

    code: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    name: str = Field(min_length=1, max_length=64)
    short_name: str = Field(default="", max_length=16)
    color: str = "#1d63f0"
    iam_endpoint: str = Field(min_length=8, max_length=255)
    domain_name: str = Field(min_length=1, max_length=128)
    domain_id: str | None = None
    auth_type: str = "PASSWORD"
    username: str | None = None
    access_key: str | None = None
    secret: str | None = None
    endpoints: dict[str, str] = {}
    enabled: bool = True
    report_mail_to: list[str] = []
    remark: str | None = None
    metrics_source: str = "AUTO"
    prometheus_url: str | None = None
    projects: list[ProjectIn] = Field(min_length=1)

    @field_validator("auth_type")
    @classmethod
    def _auth(cls, v: str) -> str:
        if v not in ("PASSWORD", "AKSK"):
            raise ValueError("认证方式只能是 PASSWORD（用户名密码）或 AKSK")
        return v

    @field_validator("metrics_source")
    @classmethod
    def _source(cls, v: str) -> str:
        if v not in SOURCES:
            raise ValueError(f"监控指标来源只能是 {'/'.join(SOURCES)}")
        return v


class ProjectOut(ProjectIn):
    id: int


class TenantOut(BaseModel):
    id: int
    code: str
    name: str
    short_name: str
    color: str
    iam_endpoint: str
    domain_name: str
    domain_id: str | None = None
    auth_type: str
    username: str | None = None
    access_key_mask: str | None = None
    secret_set: bool
    endpoints: dict[str, str]
    enabled: bool
    report_mail_to: list[str]
    remark: str | None = None
    metrics_source: str
    metrics_effective: str
    prometheus_url: str | None = None
    capabilities: dict
    last_check: dict
    last_check_at: datetime | None = None
    projects: list[ProjectOut]
    resource_count: int = 0
    user_count: int = 0
