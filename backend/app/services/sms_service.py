"""未认领告警短信升级：默认关闭、每人员单次提交、不确定结果绝不自动重试。

PROCESSING 在调用网关前提交；同一租户只允许一个在途作业，崩溃遗留超过
30 秒转为 UNKNOWN。单轮预算约 10 秒，每次请求最长 5 秒，每租户最多两次。
"""
import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import monotonic

from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import AlarmEvent, AlarmEventLog, Contact, ContactGroup, HcsAccount, NotifyRoute, SysSetting, User
from app.models.base import utcnow
from app.models.sms import SmsNotification
from app.notify.routing import route_matches
from app.notify.sms_gateway import SmsBatchResult, SmsGatewayError, send_batch
from app.services import silence_service, sms_settings
from app.services.catalog import SERVICES

PROCESSING_TIMEOUT = timedelta(seconds=30)
CYCLE_SECONDS = 10.0
REQUEST_SECONDS = 5.0
PER_TENANT_BATCH = 2
BLOCK_CODES = {"2", "11", "4", "6", "7", "30", "44"}
GATEWAY_CODES = {"0", "2", "4", "6", "7", "9", "10", "11", "30", "31", "32", "34", "41", "42",
                 "44", "45", "46", "47", "48", "99"}
DISABLED = "短信未启用或租户不可用"
OBSOLETE = "告警已认领、静默、恢复或关闭"
OLD_CYCLE = "告警早于短信启用时间或轮次已变化"
CHANGED = "短信配置或升级人员规则已变化"
SILENCED = "告警处于静默或维护窗口"
BLOCKED = "该配置已被网关拒绝，请更新租户短信配置"
UNKNOWN = "网关受理结果未知，未自动重发"
REJECTED = "短信网关拒绝受理"
CONFIG_ERROR = "短信凭据不可用，请重新保存该租户配置"


@dataclass
class _Context:
    tenant: HcsAccount
    config: sms_settings.SmsTenantConfig
    since: datetime


@dataclass
class _Recipient:
    route_id: int
    after_min: int
    user_id: int
    phone: str = field(repr=False)


def _insert(db: Session, model):
    return pg_insert(model) if db.get_bind().dialect.name == "postgresql" else sqlite_insert(model)


def _since(db: Session, alarm: AlarmEvent) -> datetime:
    reopened = db.scalar(select(func.max(AlarmEventLog.at)).where(
        AlarmEventLog.alarm_id == alarm.id, AlarmEventLog.action == "REOPEN"))
    return max(alarm.first_seen, reopened) if reopened is not None else alarm.first_seen


def _blocked(db: Session, tenant_id: int, revision: int) -> bool:
    row = db.get(SysSetting, f"sms_block:{tenant_id}", populate_existing=True)
    if row is None:
        return False
    return not isinstance(row.value, dict) or type(row.value.get("revision")) is not int or row.value["revision"] == revision


def _context(db: Session, alarm: AlarmEvent, now: datetime) -> tuple[_Context | None, str]:
    if alarm.status != "FIRING" or alarm.ack_at is not None or alarm.ack_by:
        return None, OBSOLETE
    if alarm.tenant_id is None or not sms_settings.effective(db, alarm.tenant_id):
        return None, DISABLED
    tenant = db.get(HcsAccount, alarm.tenant_id, populate_existing=True)
    config = sms_settings.load_tenant(db, alarm.tenant_id)
    activation = sms_settings.activation_time(db, alarm.tenant_id)
    since = _since(db, alarm)
    if tenant is None or not tenant.enabled:
        return None, DISABLED
    if (activation is None or config.enabled_since is None or sms_settings.load_global(db).enabled_since is None
            or since < activation or since > now):
        return None, OLD_CYCLE
    if alarm.severity not in config.severities:
        return None, "告警级别不在租户短信范围内"
    if _blocked(db, tenant.id, config.revision):
        return None, BLOCKED
    if silence_service.find_silence(db, alarm, now) is not None:
        return None, SILENCED
    return _Context(tenant, config, since), ""


def _recipients(db: Session, alarm: AlarmEvent) -> list[_Recipient]:
    users = {cid: user for cid, user in db.execute(select(Contact.id, User).join(
        User, User.id == Contact.user_id).execution_options(populate_existing=True))}
    groups = {group.code: group for group in db.scalars(select(ContactGroup).execution_options(populate_existing=True))}
    routes = db.scalars(select(NotifyRoute).where(NotifyRoute.enabled.is_(True)).order_by(
        NotifyRoute.priority, NotifyRoute.id).execution_options(populate_existing=True))
    recipients = []
    for route in routes:
        if not route_matches(route.match or {}, alarm):
            continue
        for step in route.escalation or []:
            if not isinstance(step, dict) or step.get("severities") and alarm.severity not in step["severities"]:
                continue
            after = step.get("after_min", 15)
            if type(after) is not int or after < 0:
                continue
            target = step.get("to") or "leader"
            group = groups.get(route.group_code if target == "leader" else target) if isinstance(target, str) else None
            if group is None:
                continue
            for cid in (group.leader_ids if target == "leader" else group.member_ids) or []:
                user = users.get(cid)
                if user is None or not user.enabled or not user.notify_enabled:
                    continue
                if user.tenant_ids is not None and alarm.tenant_id not in user.tenant_ids:
                    continue
                phone = sms_settings.normalize_phone(user.phone)
                if phone is not None:
                    recipients.append(_Recipient(route.id, after, user.id, phone))
        if not route.continue_match:
            break
    return sorted(recipients, key=lambda recipient: recipient.after_min)


def _dedup(alarm: AlarmEvent, recipient: _Recipient) -> str:
    identity = [alarm.id, alarm.reopen_count or 0, recipient.route_id, recipient.after_min, recipient.user_id]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def _content(alarm: AlarmEvent, context: _Context, now: datetime) -> str:
    name = context.tenant.short_name or context.tenant.name or "租户"
    name = re.sub(r"(?:https?://|www\.)\S+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"(?:\+?86[\s-]*)?1[3-9](?:[\s-]*[0-9]){9}", "", name)
    name = "".join(char for char in name if char.isalnum() or char in " _-").strip()[:24] or "租户"
    service = SERVICES.get(alarm.service, "云服务")
    minutes = min(999999, max(0, int((now - context.since).total_seconds() // 60)))
    return (f"【巡检升级】租户:{name} 等级:{alarm.severity} 服务:{service} 告警#{alarm.id} "
            f"持续{minutes}分钟未认领，请登录巡检平台处理。")


def enqueue_due(db: Session, now: datetime | None = None, batch: int = 100) -> int:
    now = now or utcnow()
    batch = max(0, min(batch, 1000))
    global_config = sms_settings.load_global(db)
    if not batch or not global_config.enabled or global_config.enabled_since is None:
        return 0
    clauses = []
    reopened = select(AlarmEventLog.alarm_id, func.max(AlarmEventLog.at).label("at")).where(
        AlarmEventLog.action == "REOPEN").group_by(AlarmEventLog.alarm_id).subquery()
    cycle = case((reopened.c.at > AlarmEvent.first_seen, reopened.c.at), else_=AlarmEvent.first_seen)
    for tenant_id in db.scalars(select(HcsAccount.id).where(HcsAccount.enabled.is_(True))):
        if not sms_settings.effective(db, tenant_id):
            continue
        config = sms_settings.load_tenant(db, tenant_id)
        activation = sms_settings.activation_time(db, tenant_id)
        if activation is not None and config.enabled_since is not None and not _blocked(db, tenant_id, config.revision):
            clauses.append(and_(AlarmEvent.tenant_id == tenant_id, cycle >= activation,
                                AlarmEvent.severity.in_(config.severities)))
    if not clauses:
        return 0
    base = select(AlarmEvent).outerjoin(reopened, reopened.c.alarm_id == AlarmEvent.id).where(
        AlarmEvent.status == "FIRING", AlarmEvent.ack_at.is_(None), AlarmEvent.ack_by.is_(None), or_(*clauses))
    count, cursor = 0, 0
    while count < batch:
        query = base.where(AlarmEvent.id > cursor).order_by(AlarmEvent.id).limit(batch)
        if db.get_bind().dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True, of=AlarmEvent)
        alarms = db.scalars(query.execution_options(populate_existing=True)).all()
        if not alarms:
            break
        for alarm in alarms:
            cursor = alarm.id
            context, _ = _context(db, alarm, now)
            if context is None:
                continue
            for recipient in _recipients(db, alarm):
                if now - context.since < timedelta(minutes=recipient.after_min):
                    continue
                statement = _insert(db, SmsNotification).values(
                    tenant_id=alarm.tenant_id, alarm_id=alarm.id, user_id=recipient.user_id,
                    alarm_generation=alarm.reopen_count or 0, dedup_key=_dedup(alarm, recipient),
                    config_revision=context.config.revision, route_id=recipient.route_id, after_min=recipient.after_min,
                    status="PENDING", content=_content(alarm, context, now),
                    phone_mask=recipient.phone[:3] + "****" + recipient.phone[-4:], created_at=now,
                ).on_conflict_do_nothing(index_elements=["dedup_key"]).returning(SmsNotification.id)
                count += db.scalar(statement) is not None
                if count >= batch:
                    break
            if count >= batch:
                break
    db.commit()
    return count


def _validate(db: Session, job: SmsNotification, now: datetime) -> tuple[_Context | None, _Recipient | None, str]:
    alarm = db.get(AlarmEvent, job.alarm_id, populate_existing=True)
    if alarm is None or alarm.tenant_id != job.tenant_id or (alarm.reopen_count or 0) != job.alarm_generation:
        return None, None, OLD_CYCLE
    context, reason = _context(db, alarm, now)
    if context is None:
        return None, None, reason
    if context.config.revision != job.config_revision:
        return None, None, CHANGED
    recipient = next((r for r in _recipients(db, alarm) if (r.route_id, r.after_min, r.user_id) == (
        job.route_id, job.after_min, job.user_id)), None)
    if recipient is None or now - context.since < timedelta(minutes=job.after_min):
        return None, None, CHANGED
    return context, recipient, ""


def _claim(db: Session, job: SmsNotification, now: datetime) -> bool:
    if db.get_bind().dialect.name == "postgresql":
        # 租户行仅锁到本次短事务提交；已提交的 PROCESSING 标记继续充当租户互斥。
        if db.scalar(select(HcsAccount.id).where(HcsAccount.id == job.tenant_id).with_for_update(skip_locked=True)) is None:
            db.commit()
            return False
        if db.scalar(select(SmsNotification.id).where(SmsNotification.id == job.id,
            SmsNotification.status == "PENDING").with_for_update(skip_locked=True)) is None:
            db.commit()
            return False
    busy = SmsNotification.__table__.alias("sms_busy")
    statement = update(SmsNotification).where(SmsNotification.id == job.id, SmsNotification.status == "PENDING", ~exists(
        select(1).select_from(busy).where(busy.c.tenant_id == job.tenant_id, busy.c.status == "PROCESSING")))
    claimed = db.scalar(statement.values(status="PROCESSING", claimed_at=now, error=None, phone_mask=None).returning(
        SmsNotification.id)) is not None
    db.commit()
    return claimed


def _skip(db: Session, job_id: int, reason: str, *, claimed: bool = False) -> None:
    db.execute(update(SmsNotification).where(SmsNotification.id == job_id,
        SmsNotification.status == ("PROCESSING" if claimed else "PENDING")).values(status="SKIPPED", error=reason))
    db.commit()


def _block(db: Session, tenant_id: int, revision: int, now: datetime) -> None:
    if sms_settings.load_tenant(db, tenant_id).revision == revision:
        value = {"revision": revision, "reason": "GATEWAY_REJECTED"}
        statement = _insert(db, SysSetting).values(key=f"sms_block:{tenant_id}", value=value,
            updated_by="sms-worker", updated_at=now)
        db.execute(statement.on_conflict_do_update(index_elements=["key"], set_={
            "value": value, "updated_by": "sms-worker", "updated_at": now}))
    db.execute(update(SmsNotification).where(SmsNotification.tenant_id == tenant_id,
        SmsNotification.config_revision == revision, SmsNotification.status == "PENDING").values(
            status="SKIPPED", error=BLOCKED))


def _result(result: SmsBatchResult, phone: str) -> tuple[str, str | None, str | None, str | None, bool]:
    if not isinstance(result, SmsBatchResult) or len(result.results) != 1:
        return "UNKNOWN", None, None, UNKNOWN, False
    recipient = result.results[0]
    if recipient.phone != phone or result.code not in GATEWAY_CODES or recipient.code not in GATEWAY_CODES:
        return "UNKNOWN", None, None, UNKNOWN, False
    code = result.code if result.code != "0" else recipient.code
    if code == "0" and recipient.accepted:
        message_id = recipient.message_id
        if message_id is not None and (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id) or phone in message_id):
            message_id = None
        return "ACCEPTED", code, message_id, None, False
    if code != "0" and not recipient.accepted:
        return "FAILED", code, None, REJECTED, code in BLOCK_CODES
    return "UNKNOWN", None, None, UNKNOWN, False


def dispatch_pending(db: Session, now: datetime | None = None,
                     sender: Callable[..., SmsBatchResult] = send_batch, batch: int = 50) -> int:
    deadline = monotonic() + CYCLE_SECONDS
    fixed_now = now
    now = now or utcnow()
    db.execute(update(SmsNotification).where(SmsNotification.status == "PROCESSING", or_(
        SmsNotification.claimed_at <= now - PROCESSING_TIMEOUT,
        and_(SmsNotification.claimed_at.is_(None), SmsNotification.created_at <= now - PROCESSING_TIMEOUT)))
        .values(status="UNKNOWN", error=UNKNOWN))
    db.commit()
    pending = select(SmsNotification.id, SmsNotification.tenant_id, func.row_number().over(
        partition_by=SmsNotification.tenant_id, order_by=SmsNotification.id).label("position")).where(
        SmsNotification.status == "PENDING").subquery()
    ids = list(db.scalars(select(pending.c.id).order_by(pending.c.position, pending.c.tenant_id, pending.c.id)
                          .limit(max(0, min(batch, 1000)))))
    accepted = 0
    attempted: Counter[int] = Counter()
    for job_id in ids:
        if monotonic() >= deadline:
            break
        at = fixed_now or utcnow()
        job = db.get(SmsNotification, job_id, populate_existing=True)
        if job is None or job.status != "PENDING":
            continue
        context, recipient, reason = _validate(db, job, at)
        if context is None or recipient is None:
            _skip(db, job_id, reason)
            continue
        if attempted[job.tenant_id] >= PER_TENANT_BATCH or deadline - monotonic() < 0.1:
            continue
        if not _claim(db, job, at):
            continue
        job = db.get(SmsNotification, job_id, populate_existing=True)
        assert job is not None
        at = fixed_now or utcnow()
        context, recipient, reason = _validate(db, job, at)
        if context is None or recipient is None:
            _skip(db, job_id, reason, claimed=True)
            continue
        alarm = db.get(AlarmEvent, job.alarm_id)
        assert alarm is not None
        content = _content(alarm, context, at)
        mask = recipient.phone[:3] + "****" + recipient.phone[-4:]
        status, block = "FAILED", False
        code: str | None = None
        message_id: str | None = None
        error: str | None = CONFIG_ERROR
        try:
            secret = sms_settings.password(context.config)
        except Exception:
            pass  # 不持久化或记录解密异常正文。
        else:
            remaining = deadline - monotonic()
            if remaining <= 0:
                _skip(db, job_id, "本轮发送预算不足，未提交网关", claimed=True)
                break
            attempted[job.tenant_id] += 1
            try:
                result = sender(context.config.endpoint, context.config.username, secret, content, [recipient.phone],
                    batch_id=job.dedup_key, expand_port=context.config.expand_port or None,
                    verify_tls=context.config.verify_tls, timeout_seconds=min(REQUEST_SECONDS, remaining))
                status, code, message_id, error, block = _result(result, recipient.phone)
            except SmsGatewayError as exc:
                block = exc.reason_code == "AUTH"
                status = "UNKNOWN" if exc.uncertain and not block else "FAILED"
                error = UNKNOWN if status == "UNKNOWN" else REJECTED
            except Exception:
                status, error = "UNKNOWN", UNKNOWN
        if block:
            _block(db, job.tenant_id, job.config_revision, at)
        finished = db.scalar(update(SmsNotification).where(SmsNotification.id == job_id,
            SmsNotification.status == "PROCESSING").values(status=status, gateway_code=code,
                gateway_message_id=message_id, error=error, content=content, phone_mask=mask,
                sent_at=at if status == "ACCEPTED" else None).returning(SmsNotification.id))
        accepted += status == "ACCEPTED" and finished is not None
        db.commit()
    return accepted
