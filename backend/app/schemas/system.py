from datetime import datetime

from pydantic import BaseModel, Field, StrictInt, field_validator

from app.metrics import SOURCES

from .common import ORMModel
from .task_execution import ExecutionOut


class TaskRunOut(ORMModel):
    id: int
    tenant_id: int | None = None
    project_ref: int | None = None
    tenant_name: str | None = None
    project_scope: str = "UNKNOWN"
    project_label: str = "历史未知项目（未记录）"
    trigger_type: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str
    resource_total: int
    abnormal_total: int
    api_calls: int
    error_summary: str | None = None
    stats: dict = Field(default_factory=dict)
    execution_id: str | None = None
    run_id: int | None = None
    request_id: str | None = None
    phase: str | None = None
    phase_label: str | None = None
    current: int | None = None
    total: int | None = None
    unit: str | None = None
    message: str | None = None
    created_at: datetime | None = None
    queued_at: datetime | None = None
    heartbeat_at: datetime | None = None
    deadline_at: datetime | None = None
    elapsed_sec: float | None = None
    timeout_sec: int | None = None
    cancel_requested: bool = False
    progress: dict = Field(default_factory=dict)
    history: list[dict] = Field(default_factory=list)


class TaskScopeOut(BaseModel):
    task_code: str
    tenant_id: int | None = None
    tenant_name: str | None = None
    project_ref: int | None = None
    project_scope: str
    project_label: str
    last_status: str
    last_at: datetime
    last_success_at: datetime | None = None
    effective_status: str | None = None
    effective_run_id: int | None = None
    fails: int = 0
    resource_total: int = 0
    abnormal_total: int = 0
    effective_finished_at: datetime | None = None
    effective_resource_total: int | None = None
    effective_abnormal_total: int | None = None


class TaskOut(BaseModel):
    code: str
    name: str
    service: str
    interval_sec: int
    cron: str | None = None
    enabled: bool
    params: dict = Field(default_factory=dict)
    effective_params: dict = Field(default_factory=dict)
    parameter_schema: dict = Field(default_factory=dict)
    next_run: datetime | str | None = None
    next_run_source: str
    types: list[str] = Field(default_factory=list)
    note: str | None = None
    checks: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    min_interval: int = 30
    skipped_tenants: list[str] = Field(default_factory=list)
    scopes: list[TaskScopeOut] = Field(default_factory=list)
    history_limit_per_scope: int = 12
    runs: list[TaskRunOut] = Field(default_factory=list)
    latest_execution: ExecutionOut | None = None
    active_execution_count: int = 0
    queued_count: int = 0


class ProjectIn(BaseModel):
    id: int | None = Field(None, ge=1)
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
    domain_name: str = Field(default="", max_length=128)
    domain_id: str | None = None
    auth_type: str = "PASSWORD"
    username: str | None = None
    access_key: str | None = None
    secret: str | None = None
    endpoints: dict[str, str] = {}
    enabled: bool = True
    report_user_ids: list[StrictInt] | None = None
    report_mail_to: list[str] = Field(default_factory=list, description="旧客户端兼容：仅接受已登记用户的邮箱")
    remark: str | None = None
    metrics_source: str = Field(
        default="AUTO", description="AUTO / CES（兼容）/ PROMETHEUS / MANAGEONE（ECS）/ NONE；AOM 已停用")
    prometheus_url: str | None = None
    projects: list[ProjectIn] = Field(min_length=1)

    @field_validator("projects")
    @classmethod
    def _unique_projects(cls, projects: list[ProjectIn]) -> list[ProjectIn]:
        ids = [p.id for p in projects if p.id is not None]
        keys = [(p.region_id, p.project_id) for p in projects]
        if len(ids) != len(set(ids)) or len(keys) != len(set(keys)):
            raise ValueError("项目 ID 或区域/项目标识不能重复")
        return projects

    @field_validator("domain_name", mode="before")
    @classmethod
    def _domain_name(cls, value: str | None) -> str:
        return (value or "").strip()

    @field_validator("auth_type")
    @classmethod
    def _auth(cls, v: str) -> str:
        if v not in ("PASSWORD", "AKSK"):
            raise ValueError("认证方式只能是 PASSWORD（用户名密码）或 AKSK")
        return v

    @field_validator("metrics_source")
    @classmethod
    def _source(cls, v: str) -> str:
        if v == "AOM":
            raise ValueError("AOM 已全局停用，请选择 AUTO、PROMETHEUS 或 NONE；ManageOne 请在 ECS 采集策略中配置")
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
    report_user_ids: list[int] = []
    report_mail_to: list[str]
    remark: str | None = None
    metrics_source: str
    metrics_effective: str
    metrics_source_disabled: bool = False
    metrics_source_note: str | None = None
    prometheus_url: str | None = None
    capabilities: dict
    last_check: dict
    last_check_at: datetime | None = None
    projects: list[ProjectOut]
    resource_count: int = 0
    user_count: int = 0
