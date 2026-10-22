"""巡检流水线：采集 → CMDB 回写 → 规则评估 → 告警状态机 → 健康度 → 记录任务运行。

多租户：run_all 按“租户 × 项目”逐个执行（每个租户一条运行记录），run_task 执行单个目标（测试中可直接注入 client）。
"""
import logging
from collections import defaultdict
from datetime import datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alarms.fingerprint import fingerprint
from app.collectors.base import Observation, RunContext
from app.collectors.registry import TASKS
from app.core.events import publish
from app.core.response import BizError, ErrorCode
from app.hcs import HcsClient, HcsError
from app.ledger.sync import LEDGER_TASK, sync_ledger
from app.models import AlarmEvent, EvalState, InspectionRun, InspectionTask, Resource, ResourceChange
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow
from app.rules.evaluator import evaluate_rule
from app.rules.loader import rules_by_target

from . import alarm_service, collect_switch, platform_watch
from .cleanup import CLEANUP_TASK, run_cleanup
from .catalog import HEALTH_BY_SEVERITY, RESOURCE_TYPES
from .tenancy import Target, legacy_target, resolve_targets

log = logging.getLogger(__name__)


def upsert_resource(db: Session, service: str, group: list[Observation], now: datetime,
                    ctx: RunContext | None = None) -> Resource:
    first = group[0]
    res = db.scalar(select(Resource).where(Resource.resource_type == first.resource_type,
                                           Resource.resource_id == first.resource_id))
    if res is None:
        res = Resource(service=service, resource_type=first.resource_type, resource_id=first.resource_id,
                       name=first.name, labels={}, attrs={}, metrics={}, expected_state={}, first_seen=now)
        db.add(res)
    if ctx is not None:
        res.tenant_id = res.tenant_id or ctx.tenant_id
        res.project_ref = res.project_ref or ctx.project_ref
        res.region = res.region or ctx.region or None
    status = next((o.status_raw for o in group if o.status_raw), None)
    if status and status != res.status_raw:
        # 台账类资源（ECS、DCS、GaussDB、MRS 集群...）的状态变化记入台账变更，资源详情里可以看到状态时间线
        if res.id is not None and res.status_raw and RESOURCE_TYPES.get(res.resource_type, {}).get("ledger"):
            db.add(ResourceChange(tenant_id=res.tenant_id, resource_ref=res.id, resource_type=res.resource_type,
                                  resource_id=res.resource_id, name=first.name, change_type="STATUS",
                                  changes=[{"field": "status_raw", "label": "状态", "old": res.status_raw,
                                            "new": status}], source="INSPECT", operator="system", detected_at=now))
        res.status_raw, res.status_since = status, now
    attrs, metrics = dict(res.attrs or {}), dict(res.metrics or {})
    for o in group:
        attrs.update(o.attrs)
        metrics.update(o.metrics)
    res.attrs, res.metrics, res.name, res.last_seen = attrs, metrics, first.name, now
    return res


def refresh_health(db: Session, resources: list[Resource]) -> None:
    ids = [r.id for r in resources]
    worst = dict(db.execute(select(AlarmEvent.resource_ref, func.min(AlarmEvent.severity))
                            .where(AlarmEvent.status.in_(("FIRING", "ACKED")), AlarmEvent.resource_ref.in_(ids))
                            .group_by(AlarmEvent.resource_ref)).all())
    for r in resources:
        sev = worst.get(r.id)
        r.health = HEALTH_BY_SEVERITY.get(sev, "OK") if sev else "OK"


def process_observations(db: Session, service: str, observations: list[Observation], ctx: RunContext,
                         now: datetime) -> tuple[list[dict], int]:
    groups: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for o in observations:
        groups[(o.resource_type, o.resource_id)].append(o)
    resources = {k: upsert_resource(db, service, g, now, ctx) for k, g in groups.items()}
    db.flush()
    rules = rules_by_target(db, {k[0] for k in groups})
    events: list[dict] = []
    extra = {"project": ctx.project_id, "region": ctx.region or None, "tenant": ctx.tenant_name or None,
             "tenant_code": ctx.account}
    for o in observations:
        res = resources[(o.resource_type, o.resource_id)]
        if not res.monitored or res.lifecycle != "ACTIVE":
            continue
        since = res.status_since or now
        facts = {**o.facts, "critical": res.critical, "expected_running": res.expected_state.get("running", True),
                 "status_duration_min": int((now - since).total_seconds() // 60)}
        for rule in rules.get((o.resource_type, o.check_item), []):
            fp = fingerprint(ctx.account, ctx.project_id, o.resource_type, o.resource_id, rule.id, o.sub_object)
            st = db.get(EvalState, fp)
            if st is None:
                st = EvalState(fingerprint=fp, data={})
                db.add(st)
                db.flush()
            data = dict(st.data or {})
            active = db.scalar(select(AlarmEvent).where(AlarmEvent.fingerprint == fp,
                                                        AlarmEvent.status.in_(ACTIVE_STATUSES)))
            decision = evaluate_rule(rule, facts, data, active.severity if active else None)
            st.data = data
            if decision.action:
                ev = alarm_service.apply_decision(db, rule=rule, decision=decision, fp=fp, resource=res, obs=o,
                                                  facts=facts, now=now, active=active,
                                                  extra={k: v for k, v in extra.items() if v})
                db.flush()
                if ev:
                    events.append(ev)
    refresh_health(db, list(resources.values()))
    db.commit()
    return events, sum(1 for r in resources.values() if r.health not in ("OK", "UNKNOWN"))


def run_task(db: Session, task_code: str, *, client: HcsClient | None = None, target: Target | None = None,
             trigger: str = "SCHEDULE") -> InspectionRun:
    """对单个租户目标执行一次任务（target 为空时按环境变量单账号兜底）。"""
    if task_code == CLEANUP_TASK:                  # 数据清理：全局执行一次，不区分租户
        return run_cleanup_task(db, trigger=trigger)
    target = target or legacy_target()
    if task_code == LEDGER_TASK:                   # 台账同步与巡检共用任务调度、运行记录
        return sync_ledger(db, client=client, target=target, trigger=trigger)
    collector = TASKS.get(task_code)
    if collector is None:
        raise BizError(ErrorCode.NOT_FOUND, f"未实现的巡检任务：{task_code}", 404)
    task = db.get(InspectionTask, task_code)
    run = InspectionRun(task_code=task_code, tenant_id=target.tenant_id, trigger_type=trigger, started_at=utcnow(),
                        status="RUNNING")
    db.add(run)
    db.commit()
    events: list[dict] = []
    client_calls = 0
    try:
        client = client or target.client()
        ctx = RunContext(client=client, account=target.code, project_id=target.project_id,
                         workspaces=list(target.workspaces), params=(task.params if task else {}) or {},
                         tenant_id=target.tenant_id, tenant_name=target.short_name, project_ref=target.project_ref,
                         region=target.region, db=db, tenant=target.tenant)
        observations = collector.collect(ctx)
        events, abnormal = process_observations(db, collector.service, observations, ctx, now=utcnow())
        run.resource_total = len({(o.resource_type, o.resource_id) for o in observations})
        run.abnormal_total = abnormal
        run.status = "PARTIAL" if ctx.errors else "SUCCESS"
        run.error_summary = "\n".join(ctx.errors[:20]) or None
        run.stats = dict(ctx.stats)
    except HcsError as e:
        db.rollback()
        run.status, run.error_summary = "FAILED", str(e)
    except Exception as e:  # noqa: BLE001
        log.exception("task %s failed", task_code)
        db.rollback()
        run.status, run.error_summary = "FAILED", f"{type(e).__name__}: {e}"
    finally:
        client_calls = client.calls if client is not None else 0
    run.finished_at, run.api_calls = utcnow(), client_calls
    db.commit()
    platform_watch.on_run(db, run)
    for ev in events:
        publish(ev)
    return run


def run_cleanup_task(db: Session, trigger: str = "SCHEDULE") -> InspectionRun:
    run = InspectionRun(task_code=CLEANUP_TASK, tenant_id=None, trigger_type=trigger, started_at=utcnow(),
                        status="RUNNING", stats={})
    db.add(run)
    db.commit()
    try:
        deleted = run_cleanup(db)
        run.status, run.stats = "SUCCESS", deleted
        run.resource_total = sum(deleted.values())
    except Exception as e:  # noqa: BLE001
        log.exception("data cleanup failed")
        db.rollback()
        run.status, run.error_summary = "FAILED", f"{type(e).__name__}: {e}"[:500]
    run.finished_at = utcnow()
    db.commit()
    platform_watch.on_run(db, run)
    return run


def skipped_targets(db: Session, task_code: str, tenant_ids: list[int] | None = None) -> list[Target]:
    """因采集开关关闭而跳过的租户目标（立即执行时提示用户）。"""
    sw = collect_switch.load(db)
    return [t for t in resolve_targets(db, tenant_ids) if not sw.task_enabled(t.tenant_id, task_code)]


def run_all(db: Session, task_code: str, *, trigger: str = "SCHEDULE", tenant_ids: list[int] | None = None,
            http: httpx.Client | None = None, mock_url: str | None = None) -> list[InspectionRun]:
    """按租户逐个执行（某个租户失败不影响其他租户）；采集开关关闭的租户跳过，不产生运行记录。"""
    if task_code == CLEANUP_TASK:
        return [run_cleanup_task(db, trigger=trigger)]
    runs = []
    sw = collect_switch.load(db)
    for t in resolve_targets(db, tenant_ids):
        if not sw.task_enabled(t.tenant_id, task_code):
            continue
        try:
            client = t.client(http=http, mock_url=mock_url)
        except HcsError as e:            # 例如租户未配置 AK/SK：记一条失败的运行记录
            run = InspectionRun(task_code=task_code, tenant_id=t.tenant_id, trigger_type=trigger, started_at=utcnow(),
                                finished_at=utcnow(), status="FAILED", error_summary=str(e), stats={})
            db.add(run)
            db.commit()
            platform_watch.on_run(db, run)
            runs.append(run)
            continue
        runs.append(run_task(db, task_code, client=client, target=t, trigger=trigger))
    return runs
