"""ECS phase policy and durable tenant/project clocks, stored in SysSetting.

Policy reads never modify CollectSwitch or task enablement. Clock operations own
their transactions and must be called outside the collector/sample transaction.
CAS updates fence expired workers; only a successful phase advances its clock.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Event, Thread
from time import sleep
from uuid import uuid4

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, load_only

from app.core.config import get_settings
from app.core.response import BizError, ErrorCode
from app.models import AlarmEvent, AlarmEventLog, AlarmRule, HcsAccount, InspectionTask, Resource, SysSetting
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow
from app.schemas.collection_policy import CollectionPolicy
from app.services import collect_switch

log = logging.getLogger(__name__)
RESOURCE_TYPE = "ecs_server"
PHASES = ("inventory", "metrics", "inspection", "quota")
TASK_PHASES = {"ecs_metrics": "metrics", "ecs_status": "inspection", "ecs_quota": "quota"}
LEASE_SEC = 900
RETRY_SEC = 60
METRIC_CHECKS = {"cpu": "cpu_util", "memory": "mem_util", "disk": "disk_util"}
ALARM_BATCH = 500


class PolicyChanged(Exception):
    """Discard results collected before a tenant policy change."""


def policy_key(tenant_id: int | None, rtype: str = RESOURCE_TYPE) -> str:
    return f"collection_policy:{tenant_id or 0}:{rtype}"


def initialize_disabled(db: Session, tenant_id: int, operator: str) -> None:
    """New tenants explicitly opt out of every phase; never overwrite an existing policy."""
    if tenant_id <= 0:
        return
    policy = CollectionPolicy.model_validate({})
    for phase in PHASES:
        getattr(policy, phase).enabled = False
    _insert(db, policy_key(tenant_id), policy.model_dump(mode="json"), operator)
    db.flush()


def effective(db: Session, tenant_id: int | None, rtype: str = RESOURCE_TYPE) -> CollectionPolicy:
    row = db.scalar(select(SysSetting).where(SysSetting.key == policy_key(tenant_id, rtype))
                    .execution_options(populate_existing=True))
    if row is not None:
        # 读取旧数据只忽略 AOM，避免失效/格式损坏的映射阻塞独立阶段；不在读取时改库。
        value = {**row.value, "aom": None}
        return CollectionPolicy.model_validate(value)
    defaults = {}
    if rtype == RESOURCE_TYPE:
        tasks = {task.code: task for task in db.scalars(select(InspectionTask).where(
            InspectionTask.code.in_(TASK_PHASES)).execution_options(populate_existing=True))}
        for code, phase in TASK_PHASES.items():
            if code in tasks:
                defaults[phase] = {"enabled": phase != "quota", "interval_sec": tasks[code].interval_sec}
        for code in ("ecs_metrics", "ecs_status"):
            params = (tasks[code].params or {}) if code in tasks else {}
            for key in ("prometheus",):
                if key in params and key not in defaults:
                    raw = params[key]
                    if key == "prometheus" and isinstance(raw, dict) and not set(raw).intersection(
                            {"queries", "instance_label", "units", "timestamp_queries", "label_filters"}):
                        raw = {"queries": raw}
                    defaults[key] = raw
    return CollectionPolicy.model_validate(defaults)


def phase_status(db: Session, tenant_id: int, policy: CollectionPolicy) -> dict:
    from app.collectors.registry import TASKS

    master = collect_switch.load(db).enabled(tenant_id, RESOURCE_TYPE)
    task_codes = {"inventory": "ledger_sync", **{phase: task for task, phase in TASK_PHASES.items()}}
    tasks = {t.code: t.enabled for t in db.scalars(select(InspectionTask).where(
        InspectionTask.code.in_(task_codes.values())))}
    schedules = {phase: schedule(db, tenant_id, phase, policy) for phase in task_codes}
    valid = {phase: cron is None or _cron_trigger(cron) is not None
             for phase, (_interval, cron, _source) in schedules.items()}
    return {phase: {"configured_enabled": getattr(policy, phase).enabled,
                    "master_enabled": master, "task_code": code, "task_enabled": bool(tasks.get(code)),
                    "supported": code == "ledger_sync" or code in TASKS,
                    "effective_enabled": bool(master and getattr(policy, phase).enabled and tasks.get(code)
                                               and (code == "ledger_sync" or code in TASKS) and valid[phase]),
                    "reason": ("unsupported" if code != "ledger_sync" and code not in TASKS else
                               "master_disabled" if not master else
                               "stage_disabled" if not getattr(policy, phase).enabled else
                               "task_disabled" if not tasks.get(code) else
                               "invalid_schedule" if not valid[phase] else "enabled"),
                    "schedule_source": schedules[phase][2], "interval_sec": schedules[phase][0],
                    "cron": schedules[phase][1]}
            for phase, code in task_codes.items()}


def schedule(db: Session, tenant_id: int | None, phase: str, policy: CollectionPolicy,
             rtype: str = RESOURCE_TYPE) -> tuple[int, str | None, str]:
    """An explicit tenant policy owns intervals; otherwise retain legacy task schedules."""
    interval = getattr(policy, phase).interval_sec
    explicit = db.scalar(select(SysSetting.key).where(SysSetting.key == policy_key(tenant_id, rtype)))
    if explicit is not None:
        return interval, None, "policy"
    code = next((task for task, task_phase in TASK_PHASES.items() if task_phase == phase), None)
    if code is not None and rtype == RESOURCE_TYPE:
        task = db.execute(select(InspectionTask.interval_sec, InspectionTask.cron).where(
            InspectionTask.code == code)).first()
        if task is not None:
            return task.interval_sec, task.cron or None, "task"
    return interval, None, "default"


def _cron_trigger(spec: str) -> CronTrigger | None:
    try:
        return CronTrigger.from_crontab(spec, timezone=get_settings().timezone)
    except ValueError:
        return None


def resource_included(resource: Resource, policy: CollectionPolicy | dict) -> bool:
    policy = CollectionPolicy.model_validate(policy)
    if (resource.resource_type != RESOURCE_TYPE or not resource.monitored
            or resource.scope_excluded or resource.lifecycle != "ACTIVE"):
        return False
    selection = policy.selection
    return (selection.mode == "all" or selection.mode == "critical" and resource.critical
            or selection.mode == "selected" and resource.id in selection.resource_ids)


def select_resources(db: Session, tenant_id: int | None, project_ref: int | None,
                     policy: CollectionPolicy | dict, rtype: str = RESOURCE_TYPE) -> list[Resource]:
    policy = CollectionPolicy.model_validate(policy)
    if not collect_switch.load(db).enabled(tenant_id, rtype):
        return []
    q = select(Resource).where(Resource.tenant_id == tenant_id, Resource.project_ref == project_ref,
                               Resource.resource_type == rtype, Resource.lifecycle == "ACTIVE",
                               Resource.monitored.is_(True), Resource.scope_excluded.is_(False))
    if policy.selection.mode == "critical":
        q = q.where(Resource.critical.is_(True))
    elif policy.selection.mode == "selected":
        # Chunking avoids SQLite bind limits and never issues a per-resource query.
        ids = policy.selection.resource_ids
        rows: list[Resource] = []
        for start in range(0, len(ids), 500):
            rows.extend(db.scalars(q.where(Resource.id.in_(ids[start:start + 500]))
                                   .execution_options(populate_existing=True)))
        return sorted(rows, key=lambda r: r.id)
    return list(db.scalars(q.order_by(Resource.id).execution_options(populate_existing=True)))


def stage_enabled(db: Session, tenant_id: int | None, phase: str,
                  policy: CollectionPolicy | dict | None = None, rtype: str = RESOURCE_TYPE) -> bool:
    policy = CollectionPolicy.model_validate(policy) if policy is not None else effective(db, tenant_id, rtype)
    if not collect_switch.load(db).enabled(tenant_id, rtype) or not getattr(policy, phase).enabled:
        return False
    code = "ledger_sync" if phase == "inventory" else next(
        (task for task, task_phase in TASK_PHASES.items() if task_phase == phase), None)
    enabled = db.scalar(select(InspectionTask.enabled).where(InspectionTask.code == code)) if code else None
    # A missing task is retained for direct legacy/test integrations. An explicit
    # disabled task is a gate, including for manual refreshes.
    return enabled is not False


def _insert(db: Session, key: str, value: dict, operator: str | None = None) -> None:
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    db.execute(insert(SysSetting).values(key=key, value=value, updated_by=operator)
               .on_conflict_do_nothing(index_elements=["key"]))


def _guard_key(tenant_id: int | None) -> str:
    # Separate from the policy: acquiring a lock must not turn legacy defaults
    # into an explicit policy or change which schedule owns the task.
    return f"collection_guard:{tenant_id or 0}:{RESOURCE_TYPE}"


def revision(db: Session, tenant_id: int | None) -> str | None:
    value = db.scalar(select(SysSetting.value).where(SysSetting.key == _guard_key(tenant_id)))
    return value.get("revision") if isinstance(value, dict) else None


def _lock_guard(db: Session, tenant_id: int | None) -> SysSetting:
    if tenant_id is not None:
        # ManageOne saves lock the tenant before alarms. Keep the same prefix:
        # tenant -> policy guard -> resources/evaluation/alarm rows. The lock
        # also prevents a credentials/source save between validation and commit.
        db.execute(select(HcsAccount.id).where(HcsAccount.id == tenant_id).with_for_update())
    key = _guard_key(tenant_id)
    # The insert also obtains SQLite's write lock. PostgreSQL serializes on the
    # guard row; both writers and workers acquire it before resource/alarm locks.
    _insert(db, key, {})
    row = db.scalar(select(SysSetting).where(SysSetting.key == key).with_for_update()
                    .execution_options(populate_existing=True))
    assert row is not None
    return row


def capture_run(db: Session, tenant_id: int | None, params: dict) -> CollectionPolicy:
    """Capture before cloud I/O, without holding an administrator's save lock."""
    current = effective(db, tenant_id)
    expected = params.get("_collection_policy")
    if expected is not None and CollectionPolicy.model_validate(expected) != current:
        raise PolicyChanged()
    params["_collection_policy"] = current.model_dump(mode="json")
    params.setdefault("_collection_policy_revision", revision(db, tenant_id))
    return current


def lock_for_run(db: Session, tenant_id: int | None, params: dict) -> CollectionPolicy:
    """Fence persistence through commit, including changes reverted during I/O."""
    guard = _lock_guard(db, tenant_id)
    current = effective(db, tenant_id)
    if "_collection_policy_revision" in params and (
            CollectionPolicy.model_validate(params["_collection_policy"]) != current
            or params["_collection_policy_revision"] != guard.value.get("revision")):
        raise PolicyChanged()
    return current


def _validate_selection(db: Session, tenant_id: int, rtype: str, policy: CollectionPolicy) -> None:
    ids = policy.selection.resource_ids
    found: set[int] = set()
    for start in range(0, len(ids), 500):
        found.update(db.scalars(select(Resource.id).where(Resource.id.in_(ids[start:start + 500]),
            Resource.tenant_id == tenant_id, Resource.resource_type == rtype, Resource.lifecycle == "ACTIVE")))
    if found != set(ids):
        raise BizError(ErrorCode.BAD_REQUEST, "所选资源不存在、已释放或不属于本租户的该资源类型")


def _close_excluded_alarms(db: Session, tenant_id: int, rtype: str, before: CollectionPolicy,
                           policy: CollectionPolicy, operator: str) -> int:
    """Close intentional policy exclusions, never missing/transient metric data.

    Scope resources by local ID and tenant, and fetch check metadata in the same
    bounded query. Only alarm rows are locked on PostgreSQL's nullable joins.
    The caller retains the policy/audit transaction; no recovery is fabricated.
    """
    inspection_off = before.inspection.enabled and not policy.inspection.enabled
    metrics_off = before.metrics.enabled and not policy.metrics.enabled
    removed_keys = set(before.metric_keys) - set(policy.metric_keys)
    disabled_checks = {check for check, key in METRIC_CHECKS.items() if metrics_off or key in removed_keys}
    selection_changed = (before.selection.mode != policy.selection.mode
        or policy.selection.mode == "selected" and set(before.selection.resource_ids) != set(policy.selection.resource_ids))
    if not inspection_off and not disabled_checks and not selection_changed:
        return 0
    query = (select(AlarmEvent, Resource, AlarmRule.check_item)
        .outerjoin(Resource, (Resource.id == AlarmEvent.resource_ref)
                   & (Resource.tenant_id == tenant_id) & (Resource.resource_type == rtype))
        .outerjoin(AlarmRule, (AlarmRule.id == AlarmEvent.rule_id) & (AlarmRule.resource_type == rtype))
        .where(AlarmEvent.tenant_id == tenant_id, AlarmEvent.resource_type == rtype,
               AlarmEvent.status.in_(ACTIVE_STATUSES))
        .options(load_only(Resource.id, Resource.tenant_id, Resource.resource_type,
                           Resource.monitored, Resource.scope_excluded, Resource.lifecycle, Resource.critical))
        .with_for_update(of=AlarmEvent).execution_options(populate_existing=True))
    if not inspection_off and not selection_changed:
        snapshot_check = getattr(AlarmEvent, "check_item", None)
        check = (func.coalesce(func.nullif(func.trim(snapshot_check), ""), AlarmRule.check_item)
                 if snapshot_check is not None else AlarmRule.check_item)
        query = query.where(check.in_(disabled_checks))
    now, closed, last_id = utcnow(), 0, 0
    db.flush()
    while True:
        rows = db.execute(query.where(AlarmEvent.id > last_id).order_by(AlarmEvent.id).limit(ALARM_BATCH)).all()
        if not rows:
            break
        for alarm, resource, rule_check in rows:
            check_item = (getattr(alarm, "check_item", None) or "").strip() or rule_check
            note = None
            if inspection_off:
                note = "已关闭 ECS 巡检阶段，行政关闭告警（非指标恢复）"
            elif selection_changed and (not resource_included(resource, policy) if resource is not None
                                        else policy.selection.mode != "all"):
                note = "ECS 监控范围已排除此资源，行政关闭告警（非指标恢复）"
            elif check_item in disabled_checks:
                reason = "已关闭 ECS 指标采集阶段" if metrics_off else f"ECS 采集策略已移除指标 {METRIC_CHECKS[check_item]}"
                note = f"{reason}，行政关闭告警（非指标恢复）"
            if note is not None:
                previous, alarm.status = alarm.status, "CLOSED"
                alarm.resolved_at = alarm.resolved_at or now
                db.add(AlarmEventLog(alarm_id=alarm.id, action="CLOSE", from_status=previous, to_status="CLOSED",
                                    operator=operator, note=note, at=now))
                closed += 1
        last_id = rows[-1][0].id
        db.flush()
        if len(rows) < ALARM_BATCH:
            break
    return closed


def save(db: Session, tenant_id: int, rtype: str, policy: CollectionPolicy, operator: str,
         *, changes: dict | None = None) -> dict:
    """Caller authorizes the tenant and commits policy + CLOSE history + audit atomically."""
    if tenant_id <= 0 or db.get(HcsAccount, tenant_id) is None:
        raise BizError(ErrorCode.NOT_FOUND, "租户不存在", 404)
    if rtype != RESOURCE_TYPE:
        raise BizError(ErrorCode.BAD_REQUEST, "当前采集策略仅支持 ECS 云服务器")
    _validate_selection(db, tenant_id, rtype, policy)
    guard = _lock_guard(db, tenant_id)
    _validate_selection(db, tenant_id, rtype, policy)
    fallback = effective(db, tenant_id, rtype)
    key = policy_key(tenant_id, rtype)
    _insert(db, key, {})
    row = db.scalar(select(SysSetting).where(SysSetting.key == key).with_for_update()
                    .execution_options(populate_existing=True))
    assert row is not None
    before = CollectionPolicy.model_validate({**row.value, "aom": None}) if row.value else fallback
    if changes is not None:
        merged = before.model_dump(mode="json")
        for key, value in changes.items():
            if key in (*PHASES, "selection") and isinstance(value, dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        policy = CollectionPolicy.model_validate(merged)
        _validate_selection(db, tenant_id, rtype, policy)
    row.value, row.updated_by = policy.model_dump(mode="json"), operator
    if before != policy:
        guard.value = {"revision": uuid4().hex}
    closed = _close_excluded_alarms(db, tenant_id, rtype, before, policy, operator)
    db.flush()
    return {"policy": policy.model_dump(mode="json"), "closed_alarms": closed,
            "previous": before.model_dump(mode="json")}


def clock_key(tenant_id: int | None, project_ref: int | None, rtype: str, phase: str) -> str:
    # SysSetting.key is VARCHAR(64), including on PostgreSQL.
    identity = f"{tenant_id}:{project_ref}:{rtype}:{phase}"
    return "collection_clock:" + sha256(identity.encode()).hexdigest()[:40]


def _at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _due(value: dict, interval_sec: int, now: datetime, cron: str | None = None) -> bool:
    lease = _at(value.get("lease_until"))
    if lease is not None and lease > now:
        return False
    retry = _at(value.get("retry_at"))
    if retry is not None:
        return retry <= now
    last = _at(value.get("last_success"))
    if cron:
        trigger = _cron_trigger(cron)
        if trigger is None:
            return False
        # A new scheduler does not backfill a never-run daily cron. Existing
        # clocks coalesce missed fires within the legacy one-hour grace window.
        start = now - timedelta(seconds=3600 if last is not None else 60)
        if last is not None:
            start = max(start, last + timedelta(microseconds=1))
        following = trigger.get_next_fire_time(None, start)
        return following is not None and following <= now
    return last is None or last + timedelta(seconds=interval_sec) <= now


def due(db: Session, tenant_id: int | None, project_ref: int | None, phase: str,
        policy: CollectionPolicy | None = None, *, rtype: str = RESOURCE_TYPE, now: datetime | None = None) -> bool:
    policy = policy or effective(db, tenant_id, rtype)
    if not stage_enabled(db, tenant_id, phase, policy, rtype):
        return False
    value = db.scalar(select(SysSetting.value).where(SysSetting.key == clock_key(tenant_id, project_ref, rtype, phase)))
    interval, cron, _source = schedule(db, tenant_id, phase, policy, rtype)
    return _due(_live_clock(db, value or {}), interval, now or utcnow(), cron)


def _live_clock(db: Session, value: dict) -> dict:
    """持久执行已结束时，旧阶段租约不能再挡住下一次执行。"""
    execution_id = value.get("execution_id")
    if not execution_id:
        return value
    from app.models import TaskExecution

    row = db.get(TaskExecution, execution_id)
    if row is None or row.status not in ("QUEUED", "RUNNING"):
        return {key: entry for key, entry in value.items() if key not in ("token", "lease_until", "execution_id")}
    return value


@dataclass(frozen=True)
class Claim:
    key: str
    token: str


def _claim(db: Session, tenant_id: int | None, project_ref: int | None, phase: str,
           policy: CollectionPolicy | None = None, *, rtype: str = RESOURCE_TYPE,
           force: bool = False, now: datetime | None = None) -> Claim | None:
    policy, now = policy or effective(db, tenant_id, rtype), now or utcnow()
    if not stage_enabled(db, tenant_id, phase, policy, rtype):
        return None
    key = clock_key(tenant_id, project_ref, rtype, phase)
    interval, cron, _source = schedule(db, tenant_id, phase, policy, rtype)
    _insert(db, key, {"tenant_id": tenant_id, "project_ref": project_ref, "resource_type": rtype, "phase": phase})
    for _ in range(5):
        value = db.scalar(select(SysSetting.value).where(SysSetting.key == key)) or {}
        live_value = _live_clock(db, value)
        lease = _at(live_value.get("lease_until"))
        if (lease is not None and lease > now) or (not force and not _due(live_value, interval, now, cron)):
            db.commit()
            return None
        token = uuid4().hex
        from .execution_runtime import current

        control = current()
        changed = db.scalar(update(SysSetting).where(SysSetting.key == key, SysSetting.value == value)
            .values(value={**live_value, **({"execution_id": control.execution_id} if control else {}),
                           "token": token, "lease_until": (now + timedelta(seconds=LEASE_SEC)).isoformat(),
                           "started_at": now.isoformat()}).returning(SysSetting.key),
            execution_options={"synchronize_session": False})
        db.commit()
        if changed:
            return Claim(key, token)
    return None


def _transient(exc: OperationalError) -> bool:
    original = exc.orig
    return (getattr(original, "sqlstate", None) in ("40001", "40P01")
            or "locked" in str(original).lower() or "busy" in str(original).lower())


def claim(db: Session, tenant_id: int | None, project_ref: int | None, phase: str,
          policy: CollectionPolicy | None = None, *, rtype: str = RESOURCE_TYPE,
          force: bool = False, now: datetime | None = None) -> Claim | None:
    for attempt in range(5):
        try:
            return _claim(db, tenant_id, project_ref, phase, policy, rtype=rtype, force=force, now=now)
        except OperationalError as exc:
            db.rollback()
            if not _transient(exc) or attempt == 4:
                raise
            sleep(0.01 * 2 ** attempt)
    return None


def _finish(db: Session, claimed: Claim, *, success: bool | None, now: datetime) -> bool:
    for _ in range(5):
        value = db.scalar(select(SysSetting.value).where(SysSetting.key == claimed.key)) or {}
        if value.get("token") != claimed.token:
            return False
        if success is None and _live_clock(db, value) != value:
            return False
        new = dict(value)
        if success is None:
            new["lease_until"] = (now + timedelta(seconds=LEASE_SEC)).isoformat()
        else:
            new.pop("token", None)
            new.pop("lease_until", None)
            if success:
                new["last_success"] = now.isoformat()
                new.pop("retry_at", None)
            else:
                new["retry_at"] = (now + timedelta(seconds=RETRY_SEC)).isoformat()
        changed = db.scalar(update(SysSetting).where(SysSetting.key == claimed.key, SysSetting.value == value)
            .values(value=new).returning(SysSetting.key), execution_options={"synchronize_session": False})
        db.commit()
        if changed:
            return True
    return False


def finish(db: Session, claimed: Claim, success: bool, *, now: datetime | None = None) -> bool:
    for attempt in range(5):
        try:
            return _finish(db, claimed, success=success, now=now or utcnow())
        except OperationalError as exc:
            db.rollback()
            if not _transient(exc) or attempt == 4:
                raise
            sleep(0.01 * 2 ** attempt)
    return False


@contextmanager
def maintain_claim(db: Session, claimed: Claim) -> Iterator[None]:
    """Renew a long running phase using an independent session; never share Session across threads."""
    stopped = Event()
    bind = db.get_bind()
    thread = None
    # StaticPool memory fixtures share a single connection; run synchronously there.
    in_memory = bind.dialect.name == "sqlite" and getattr(getattr(bind, "url", None), "database", None) in (
        None, "", ":memory:")
    if not in_memory:
        def renew() -> None:
            while not stopped.wait(LEASE_SEC / 3):
                try:
                    with Session(bind=bind) as session:
                        if not _finish(session, claimed, success=None, now=utcnow()):
                            return
                except Exception as exc:
                    log.error("collection_clock_renewal_failed", extra={"exception_type": type(exc).__name__})
                    return
        thread = Thread(target=renew, name="collection-clock", daemon=True)
        thread.start()
    try:
        yield
    finally:
        stopped.set()
        if thread:
            thread.join(timeout=5)
