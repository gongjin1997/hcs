from datetime import datetime

from pydantic import BaseModel

from .common import ORMModel


class AlarmOut(ORMModel):
    id: int
    rule_id: str
    service: str
    resource_type: str
    resource_name: str
    resource_ref: int | None = None
    tenant_id: int | None = None
    sub_object: str = ""
    severity: str
    status: str
    suppress_reason: str | None = None
    silence_id: int | None = None
    title: str
    check_item: str
    current_value: str | None = None
    threshold_text: str | None = None
    owner: str | None = None
    first_seen: datetime
    last_seen: datetime
    resolved_at: datetime | None = None
    count: int
    ack_by: str | None = None
    ack_at: datetime | None = None


class AlarmLogOut(ORMModel):
    id: int
    action: str
    from_status: str | None = None
    to_status: str | None = None
    operator: str
    note: str | None = None
    at: datetime


class NotificationOut(ORMModel):
    id: int
    kind: str
    channel: str
    recipients: list[str]
    subject: str | None = None
    status: str
    sent_at: datetime | None = None
    error: str | None = None
    retry: int = 0
    next_retry_at: datetime | None = None
    message_id: str | None = None


class AlarmDetailOut(AlarmOut):
    fingerprint: str
    detail: dict
    runbook: str | None = None
    logs: list[AlarmLogOut] = []
    notifications: list[NotificationOut] = []
    silence: dict | None = None              # 命中的静默 / 维护窗口（已抑制时）


class AlarmStats(BaseModel):
    FIRING: int = 0
    ACKED: int = 0
    SUPPRESSED: int = 0
    P1: int = 0
    P2: int = 0
    P3: int = 0
    P4: int = 0
