from datetime import datetime

from pydantic import BaseModel

from .common import ORMModel


class RuleOut(ORMModel):
    id: str
    name: str
    resource_type: str
    check_item: str
    kind: str
    scope: dict
    expr: dict
    for_count: int
    recover: dict
    repeat_interval_sec: int | None = None
    runbook: str | None = None
    enabled: bool
    version: int
    updated_by: str | None = None
    updated_at: datetime | None = None


class RuleIn(BaseModel):
    name: str
    kind: str
    scope: dict = {}
    expr: dict
    for_count: int = 1
    recover: dict = {}
    repeat_interval_sec: int | None = None
    runbook: str | None = None
    enabled: bool = True


class DryRunIn(BaseModel):
    kind: str
    expr: dict
    recover: dict = {}
    facts: dict


class DryRunOut(BaseModel):
    matched: bool
    severity: str | None = None
    recovered: bool
    message: str
