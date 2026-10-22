from datetime import date as Date
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .common import ORMModel

ReportKind = Literal["DAILY", "WEEKLY", "MONTHLY", "LEDGER"]


class ReportBrief(ORMModel):
    id: int
    kind: str
    tenant_id: int | None = None
    tenant_name: str | None = None
    title: str
    period_start: datetime
    period_end: datetime
    summary: dict
    created_by: str
    created_at: datetime
    sent_to: list
    sent_at: datetime | None = None
    send_error: str | None = None


class ReportOut(ReportBrief):
    data: dict
    default_to: list[str] = []


class ReportIn(BaseModel):
    kind: ReportKind
    date: Date | None = Field(None, description="周期内任意一天；为空时取上一个完整周期（昨天 / 上周 / 上月）")
    tenant_id: int | None = Field(None, description="为空 = 全部租户合并报表")


class SendIn(BaseModel):
    to: list[str] | None = Field(None, description="收件人；为空时使用租户报表收件人 / REPORT_MAIL_TO")

    @field_validator("to")
    @classmethod
    def _emails(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out = [x.strip() for x in v if x.strip()]
        bad = [x for x in out if "@" not in x or x.startswith("@") or x.endswith("@")]
        if bad:
            raise ValueError(f"邮箱格式不正确：{', '.join(bad)}")
        return out
