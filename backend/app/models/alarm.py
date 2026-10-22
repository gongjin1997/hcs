from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JsonType, UtcDateTime

from .base import utcnow

ACTIVE_STATUSES = ("FIRING", "ACKED", "SUPPRESSED")
_ACTIVE_SQL = "status IN ('FIRING', 'ACKED', 'SUPPRESSED')"


class AlarmRule(Base):
    __tablename__ = "alarm_rule"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)    # 例如 R-DCS-005
    name: Mapped[str] = mapped_column(String(128))
    resource_type: Mapped[str] = mapped_column(String(32))
    check_item: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))                    # threshold / state / event
    scope: Mapped[dict] = mapped_column(JsonType, default=dict)
    expr: Mapped[dict] = mapped_column(JsonType)                     # {"when", "severity"} 或 {"value", "levels"}
    for_count: Mapped[int] = mapped_column(Integer, default=1)
    recover: Mapped[dict] = mapped_column(JsonType, default=dict)
    repeat_interval_sec: Mapped[int | None] = mapped_column(Integer)
    runbook: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow, onupdate=utcnow)


class AlarmEvent(Base):
    __tablename__ = "alarm_event"
    __table_args__ = (
        Index("uk_alarm_active", "fingerprint", unique=True,
              postgresql_where=text(_ACTIVE_SQL), sqlite_where=text(_ACTIVE_SQL)),
        Index("idx_alarm_status_sev", "status", "severity"),
        Index("idx_alarm_tenant_status", "tenant_id", "status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(40))
    rule_id: Mapped[str] = mapped_column(String(64))
    resource_ref: Mapped[int | None] = mapped_column(Integer)
    service: Mapped[str] = mapped_column(String(32))
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_name: Mapped[str] = mapped_column(String(255))
    sub_object: Mapped[str] = mapped_column(String(128), default="")
    severity: Mapped[str] = mapped_column(String(4))                 # P1 / P2 / P3 / P4
    status: Mapped[str] = mapped_column(String(16))                  # FIRING/ACKED/SUPPRESSED/RESOLVED/CLOSED
    suppress_reason: Mapped[str | None] = mapped_column(String(64))
    silence_id: Mapped[int | None] = mapped_column(Integer)          # 命中的静默 / 维护窗口（SUPPRESSED 时）
    title: Mapped[str] = mapped_column(String(255))
    check_item: Mapped[str] = mapped_column(String(64))
    current_value: Mapped[str | None] = mapped_column(String(64))
    threshold_text: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JsonType, default=dict)
    owner: Mapped[str | None] = mapped_column(String(64))
    first_seen: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    count: Mapped[int] = mapped_column(Integer, default=1)
    reopen_count: Mapped[int] = mapped_column(Integer, default=0)
    ack_by: Mapped[str | None] = mapped_column(String(64))
    ack_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    notify_seq: Mapped[int] = mapped_column(Integer, default=0)


class AlarmEventLog(Base):
    __tablename__ = "alarm_event_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    alarm_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(32))     # FIRE/UPGRADE/NOTIFY/ACK/SILENCE/UNSILENCE/RESOLVE/REOPEN/CLOSE
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    operator: Mapped[str] = mapped_column(String(64), default="system")
    note: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class EvalState(Base):
    """规则评估状态（连续命中次数等）。生产环境可改存 Redis。"""

    __tablename__ = "eval_state"
    fingerprint: Mapped[str] = mapped_column(String(40), primary_key=True)
    data: Mapped[dict] = mapped_column(JsonType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow, onupdate=utcnow)
