from datetime import datetime

from pydantic import BaseModel

from .common import ORMModel


class ResourceOut(ORMModel):
    id: int
    tenant_id: int | None = None
    service: str
    resource_type: str
    resource_id: str
    name: str
    status_raw: str | None = None
    health: str
    region: str | None = None
    az: str | None = None
    ip: str | None = None
    spec: str | None = None
    vcpus: int | None = None
    memory_gb: float | None = None
    storage_gb: float | None = None
    created_time: datetime | None = None
    labels: dict
    attrs: dict
    metrics: dict
    expected_state: dict
    owner: str | None = None
    business: str | None = None
    env_type: str | None = None
    remark: str | None = None
    monitored: bool
    critical: bool
    lifecycle: str
    first_seen: datetime
    last_seen: datetime
    deleted_at: datetime | None = None
    status_since: datetime | None = None
    active_alarms: int = 0


class ResourcePatch(BaseModel):
    """台账管理字段（同步不会覆盖）。"""

    owner: str | None = None
    business: str | None = None
    env_type: str | None = None
    remark: str | None = None
    critical: bool | None = None
    monitored: bool | None = None
    expected_state: dict | None = None


class ChangeOut(ORMModel):
    id: int
    tenant_id: int | None = None
    resource_ref: int
    resource_type: str
    resource_id: str
    name: str
    change_type: str
    changes: list
    source: str
    operator: str
    detected_at: datetime
