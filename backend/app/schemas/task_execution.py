"""Public execution contract. Worker leases and unfiltered snapshots are never public."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

ExecutionStatus = Literal[
    "QUEUED", "RUNNING", "SUCCESS", "PARTIAL", "FAILED", "TIMEOUT", "INTERRUPTED", "CANCELLED", "SKIPPED",
]
MRSQueryMode = Literal["active_and_recent", "active_only", "recent_only", "all_history"]
MRSOffsetMode = Literal["page", "offset"]
MRSElapsedTimeUnit = Literal["milliseconds", "seconds"]


class MRSExecutionOptionsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_mode: MRSQueryMode
    submitted_window_hours: float | None = None
    submitted_time_begin: int | None = None
    submitted_time_end: int | None = None
    page_size: int
    max_pages: int
    max_items: int
    max_calls: int
    max_clusters: int
    job_offset_mode: MRSOffsetMode
    elapsed_time_unit: MRSElapsedTimeUnit | None = None


class ExecutionOptionsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_sec: int
    p90_min_samples: int | None = None
    mrs: MRSExecutionOptionsOut | None = None


class TaskRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_refs: list[Annotated[StrictInt, Field(ge=1)]] | None = Field(None, min_length=1, max_length=1000)
    idempotency_key: str | None = Field(None, min_length=1, max_length=128)

    @field_validator("project_refs")
    @classmethod
    def unique_projects(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("项目不能重复")
        return value


class ExecutionOut(BaseModel):
    id: str
    execution_id: str
    run_id: int
    task_code: str
    task_name: str
    service: str
    tenant_id: int | None = None
    tenant_name: str | None = None
    project_ref: int | None = None
    project_label: str
    request_id: str
    status: ExecutionStatus
    trigger_type: str
    phase: str
    phase_label: str
    current: int | None = None
    total: int | None = None
    unit: str | None = None
    message: str | None = None
    created_at: datetime
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    lease_expired: bool = False
    deadline_at: datetime | None = None
    elapsed_sec: float
    timeout_sec: int
    options: ExecutionOptionsOut
    cancel_requested: bool
    api_calls: int
    resource_total: int
    abnormal_total: int
    error_summary: str | None = None
    progress: dict = Field(default_factory=dict)
    history: list[dict] = Field(default_factory=list)


class ExecutionAdmissionOut(BaseModel):
    execution_ids: list[str]
    runs: list[ExecutionOut]
    skipped: list[str]
    status: ExecutionStatus
    deduplicated: bool
    request_id: str


class ExecutionTargetOut(BaseModel):
    tenant_id: int | None = None
    tenant_name: str | None = None
    project_ref: int | None = None
    project_label: str
    enabled: bool
    executable: bool
    reason: str | None = None
    active_execution_id: str | None = None


class ExecutionTargetsOut(BaseModel):
    task_code: str
    task_enabled: bool
    targets: list[ExecutionTargetOut]
