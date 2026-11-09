"""巡检流水线：采集 → CMDB 回写 → 规则评估 → 告警状态机 → 健康度 → 记录任务运行。

多租户：run_all 按“租户 × 项目”逐个执行（每个租户一条运行记录），run_task 执行单个目标（测试中可直接注入 client）。
"""
import logging
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.alarms.fingerprint import fingerprint
from app.collectors.base import Observation, RunContext
from app.collectors.registry import TASKS
from app.core.events import publish
from app.core.logging import bind_log_context, log_context
from app.core.response import BizError, ErrorCode
from app.hcs import HcsClient, HcsError
from app.hcs.client import metrics_delta
from app.ledger.fetchers import gaussdb_spec
from app.ledger.sync import LEDGER_TASK, sync_ledger
from app.metrics.ecs_diagnostics import log_summary as ecs_metrics_log_summary
from app.models import (
    AlarmEvent,
    DcsMemorySample,
    EvalState,
    InspectionRun,
    InspectionTask,
    Notification,
    Resource,
    ResourceChange,
)
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow
from app.notify.routing import load_routing
from app.rules.evaluator import Decision, evaluate_rule
from app.rules.loader import rules_by_target
from app.schemas.collection_policy import CollectionPolicy

from . import (
    alarm_service,
    cce_settings,
    collect_scope,
    collect_switch,
    collection_policy,
    inspection_evidence,
    platform_watch,
    settings_service,
    silence_service,
)
from .catalog import HEALTH_BY_SEVERITY, RESOURCE_TYPES
from .cleanup import CLEANUP_TASK, run_cleanup
from .inspection_health import inspection_state
from .tenancy import Target, legacy_target, resolve_targets

log = logging.getLogger(__name__)


class _CollectionPolicyChanged(Exception):
    """A configuration change invalidated the in-flight collector transaction."""


def _log_failure(event: str, exc: Exception, **fields: object) -> None:
    # 保留调用栈和异常类型，不把云端响应文本、认证信息或 SQL 参数拼进日志。
    safe = RuntimeError(type(exc).__name__)
    log.error(event, extra={"event": event, "exception_type": type(exc).__name__, **fields},
              exc_info=(RuntimeError, safe, exc.__traceback__))


def _log_run(run: InspectionRun, started: float, **fields: object) -> None:
    level = {"FAILED": logging.ERROR, "PARTIAL": logging.WARNING, "SKIPPED": logging.DEBUG}.get(
        run.status, logging.INFO)
    hcs = (run.stats or {}).get("hcs", {})
    fields.setdefault("error_count", len((run.error_summary or "").splitlines()))
    if run.task_code == "ecs_metrics":
        fields.update(ecs_metrics_log_summary(run.stats or {}))
    log.log(level, "task_finished", extra={
        "event": "task_finished", "task": run.task_code, "run_id": run.id, "tenant_id": run.tenant_id,
        "trigger": run.trigger_type, "status": run.status, "cost_ms": round((perf_counter() - started) * 1000, 2),
        "resource_count": run.resource_total, "abnormal_count": run.abnormal_total, "api_calls": run.api_calls,
        "upstream_errors": hcs.get("errors", 0), "rate_limit_ms": hcs.get("rate_limit_ms", 0), **fields})


def upsert_resource(db: Session, service: str, group: list[Observation], now: datetime,
                    ctx: RunContext | None = None, known: dict[tuple[str, str], Resource] | None = None) -> Resource:
    first = group[0]
    res = known.get((first.resource_type, first.resource_id)) if known is not None else db.scalar(
        select(Resource).where(Resource.resource_type == first.resource_type,
                                           Resource.resource_id == first.resource_id,
                                           Resource.project_ref == (ctx.project_ref if ctx else None),
                                           Resource.tenant_id == (ctx.tenant_id if ctx else None)))
    if res is None:
        values = dict(service=service, resource_type=first.resource_type, resource_id=first.resource_id,
                      name=first.name, labels={}, attrs={}, metrics={}, expected_state={}, first_seen=now,
                      tenant_id=ctx.tenant_id if ctx else None, project_ref=ctx.project_ref if ctx else None)
        if first.resource_type == "gaussdb_instance":
            # 首次发现时没有旧行可锁。分项任务同时发现同一实例，竞争由自然键唯一索引裁决；
            # 不回滚整个事务、不覆盖对方已写入的属性，插入后重读并锁定最新行。
            insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
            db.execute(insert(Resource).values(**values).on_conflict_do_nothing())
            res = db.scalar(select(Resource).where(Resource.resource_type == first.resource_type,
                Resource.resource_id == first.resource_id, Resource.project_ref == values["project_ref"],
                Resource.tenant_id == values["tenant_id"]).with_for_update().execution_options(populate_existing=True))
            if res is None:
                raise ValueError("GaussDB 实例标识与已有租户/项目归属冲突")
        else:
            res = Resource(**values)
            db.add(res)
    if ctx is not None:
        res.region = res.region or ctx.region or None
    status = next((o.status_raw for o in group if o.status_raw), None)
    identity = inspection_evidence.EXECUTION_ID_FIELDS.get(first.resource_type)
    changed_execution = identity is not None and any(
        identity in o.attrs and str(o.attrs[identity]) != str((res.attrs or {}).get(identity)) for o in group)
    if changed_execution:
        res.status_since = now
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
        if first.resource_type == "ecs_server" and ctx is not None and ctx.params.get("_collection_phase") == "inspection":
            # 独立状态轮询不能用较早读取的值覆盖刚完成的指标采样。
            metrics.update({key: value for key, value in o.metrics.items()
                            if key not in ("cpu_util", "mem_util", "disk_util", "disk_mount")})
        else:
            metrics.update(o.metrics)
    managed = inspection_evidence.runtime_metric_keys(first.resource_type)
    if managed:
        measured = {key: value for observation in group for key, value in observation.metrics.items()}
        metrics.update({key: measured.get(key) for key in managed})
        attrs["metrics_monitoring"] = {
            "observed_at": now.isoformat(), "time_basis": "LOCAL_COLLECTION",
            "stale_after_sec": ctx.params.get("_inspection_stale_after_sec", 900) if ctx is not None else 900}
    if first.resource_type == "ecs_server" and ctx is not None and ctx.params.get("_collection_phase") == "inspection":
        from app.metrics.ecs_readings import ecs_source
        from app.services.ecs_monitoring import config_signature, parse_at

        meta = attrs.get("ecs_monitoring")
        latest = meta.get("latest") if isinstance(meta, dict) else {}
        latest = latest if isinstance(latest, dict) else {}
        policy = ctx.params.get("_collection_policy") or {}
        max_age = policy.get("stale_after_sec", 900)
        source = ecs_source(ctx.tenant, policy, client=ctx.client)
        current = (source in ("PROMETHEUS", "CES", "MANAGEONE") and isinstance(meta, dict) and meta.get("source") == source
                   and meta.get("config_signature") == config_signature(ctx.tenant, policy, source))
        for key in ("cpu_util", "mem_util", "disk_util"):
            point = latest.get(key)
            at = parse_at(point.get("observed_at")) if isinstance(point, dict) else None
            if (not current or not isinstance(point, dict) or point.get("source") != source or at is None
                    or not now-timedelta(seconds=max_age) <= at <= now+timedelta(seconds=30)):
                metrics[key] = None
                if key == "disk_util":
                    metrics["disk_mount"] = None
    res.attrs, res.metrics, res.name, res.last_seen = attrs, metrics, first.name, now
    if first.resource_type == "gaussdb_instance":
        observed_attrs = {key: value for observation in group for key, value in observation.attrs.items()}
        if observed_attrs.get("memory_unit_verified") is False:
            res.memory_gb = None
        elif observed_attrs.get("memory_unit_verified") is True:
            res.memory_gb = attrs.get("memory_size")
        if observed_attrs.get("volume_unit_verified") is False:
            res.storage_gb = None
        if observed_attrs.get("memory_unit_verified") is False or observed_attrs.get("volume_unit_verified") is False:
            res.spec = gaussdb_spec(attrs)
    return res


def refresh_health(db: Session, resources: list[Resource], now: datetime | None = None) -> None:
    ids = [r.id for r in resources]
    if not ids:
        return
    worst = dict(db.execute(select(AlarmEvent.resource_ref, func.min(AlarmEvent.severity))
                            .join(Resource, (Resource.id == AlarmEvent.resource_ref)
                                  & AlarmEvent.tenant_id.is_not_distinct_from(Resource.tenant_id))
                            .where(AlarmEvent.status.in_(("FIRING", "ACKED")), AlarmEvent.resource_ref.in_(ids))
                            .group_by(AlarmEvent.resource_ref)).all())
    for r in resources:
        sev = worst.get(r.id)
        r.health = HEALTH_BY_SEVERITY[sev] if sev else "OK" if inspection_state(r, now) == "FRESH" else "UNKNOWN"


def _interrupt_task(db: Session, target: Target, task_code: str, now: datetime) -> None:
    context = inspection_evidence.ScopeContext(target.code, target.project_id, target.tenant_id, target.project_ref,
                                               {"_task_code": task_code})
    _inspection_task_scope(db, context)
    if "ecs_server" in inspection_evidence.task_types(context):
        context.params["_collection_policy"] = collection_policy.effective(db, target.tenant_id).model_dump(mode="json")
    rules = rules_by_target(db, inspection_evidence.task_types(context))
    _states, resources, counts = inspection_evidence.interrupt_missing(db, context, rules, [], now)
    inspection_evidence.mark_missing_resources(resources, counts, context)
    refresh_health(db, list(resources.values()), now)


def _inspection_task_scope(db: Session, ctx: RunContext | inspection_evidence.ScopeContext) -> None:
    if "gaussdb_instance" in inspection_evidence.task_types(ctx):
        configured = db.execute(select(InspectionTask.code, InspectionTask.enabled).where(
            InspectionTask.code.in_(("gaussdb_status", "gaussdb_storage", "gaussdb_components")))).all()
        if configured:
            ctx.params["_inspection_tasks"] = [code for code, enabled in configured if enabled]
    db.flush()


def _sync_scope(db: Session, ctx: RunContext, target: Target, task_code: str) -> None:
    """采集前读取采集范围，并按最新规则重算“范围外”标记（新同步的资源、改过台账字段的资源）。

    标记同步是辅助步骤，不能阻断采集：失败时回滚、记一条局部错误（任务记为部分成功），
    采集器和流水线仍会按规则实时过滤，只是已有记录的标记要等下一轮才更新。
    """
    ctx.scopes = collect_scope.load(db)
    types = [t for t in collect_switch.TASK_TYPES.get(task_code, []) if t in RESOURCE_TYPES]
    try:
        collect_scope.sync_flags(db, target.tenant_id, types, project_ref=target.project_ref, scopes=ctx.scopes)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _log_failure("scope_sync_failed", exc)
        ctx.errors.append("采集范围标记同步失败，本轮仅按规则实时过滤")


def _dcs_memory_values(metrics: dict) -> dict[str, float | None] | None:
    """只接收本次 metrics 的有限数值；可选 MB 缺失保持 NULL，不使用旧快照或补零。"""
    values: dict[str, float | None] = {}
    for key in ("memory_usage", "used_memory_mb", "max_memory_mb"):
        raw = metrics.get(key)
        if raw is None and key != "memory_usage":
            values[key] = None
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        try:
            value = float(raw)
        except (OverflowError, ValueError):
            return None
        if not math.isfinite(value) or value < 0:
            return None
        values[key] = value
    usage, maximum = values["memory_usage"], values["max_memory_mb"]
    if usage is None or (maximum is not None and maximum <= 0):
        return None
    return values


def _save_dcs_memory_samples(db: Session, groups: dict[tuple[str, str], list[Observation]],
                             resources: dict[tuple[str, str], Resource], ctx: RunContext, now: datetime) -> None:
    samples: dict[int, dict] = {}
    for key, observations in groups.items():
        if key[0] != "dcs_instance":
            continue
        res = resources[key]
        if (not res.monitored or res.lifecycle != "ACTIVE"
                or res.tenant_id != ctx.tenant_id or res.project_ref != ctx.project_ref):
            continue
        for observation in observations:
            if observation.check_item != "memory":
                continue
            values = _dcs_memory_values(observation.metrics)
            if values is not None:
                samples[res.id] = {"resource_ref": res.id, "tenant_id": res.tenant_id, "project_ref": res.project_ref,
                                   "observed_at": observation.metric_observed_at or now, **values}
    if not samples or not collect_switch.load(db).enabled(ctx.tenant_id, "dcs_instance"):
        return
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    rows = list(samples.values())
    for start in range(0, len(rows), 100):
        statement = insert(DcsMemorySample).values(rows[start:start + 100]).on_conflict_do_nothing(
            index_elements=["resource_ref", "observed_at"])
        db.execute(statement)


def process_observations(db: Session, service: str, observations: list[Observation], ctx: RunContext,
                         now: datetime) -> tuple[list[dict], int]:
    db.flush()
    switches = collect_switch.load(db)
    observations = [o for o in observations if switches.enabled(ctx.tenant_id, o.resource_type)]
    groups: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for o in observations:
        groups[(o.resource_type, o.resource_id)].append(o)
    known: dict[tuple[str, str], Resource] = {}
    ids = sorted({k[1] for k in groups})
    types = {k[0] for k in groups}
    # Gauss 分项任务还会给未返回的同范围对象标记缺采；统一先锁完整租户/项目范围，
    # 避免部分清单先锁 B、完整清单先锁 A，随后各自等待对方的锁。
    batches = [None] if service == "gaussdb" else [ids[start:start + 500] for start in range(0, len(ids), 500)]
    for batch in batches:
        query = select(Resource).where(Resource.project_ref == ctx.project_ref, Resource.tenant_id == ctx.tenant_id,
            Resource.resource_type.in_(("gaussdb_instance",) if service == "gaussdb" else types))
        if batch is not None:
            query = query.where(Resource.resource_id.in_(batch))
        for res in db.scalars(query.order_by(Resource.resource_id, Resource.resource_type,
                Resource.id).with_for_update().execution_options(populate_existing=True)):
            known[(res.resource_type, res.resource_id)] = res
    if service == "gaussdb":
        # 资源锁之后重读启停，避免等待云接口/并发写入期间保留下来的旧配置覆盖最新设置。
        _inspection_task_scope(db, ctx)
        if "_inspection_tasks" in ctx.params and ctx.params.get("_task_code") not in ctx.params["_inspection_tasks"]:
            raise _CollectionPolicyChanged()
    # 采集范围的最终判定：范围外的对象不评估、不告警；已有记录的就地标记并关闭活动告警。
    # 云请求可能耗时很久；采集前的快照只用于裁剪，最终判定必须读取管理员最新保存的范围。
    in_scope = collect_scope.filter_groups(db, ctx.tenant_id, groups, known, collect_scope.load(db))
    if len(in_scope) != len(groups):
        ctx.stats["out_of_scope"] = len(groups) - len(in_scope)
        observations = [o for o in observations if (o.resource_type, o.resource_id) in in_scope]
    phase = ctx.params.get("_collection_phase")
    ecs_target = "ecs_server" in types | inspection_evidence.task_types(ctx)
    policy = collection_policy.effective(db, ctx.tenant_id) if ecs_target else CollectionPolicy.model_validate({})
    if ecs_target or phase in ("metrics", "inspection", "quota"):
        ctx.params["_collection_policy"] = policy.model_dump(mode="json")
    ecs_phase_enabled = "ecs_server" in types and collection_policy.stage_enabled(
        db, ctx.tenant_id, phase or "inspection", policy)
    # Inventory alone owns discovery. Runtime monitoring only touches selected,
    # active local hosts; an unselected host is never a deletion candidate.
    observations = [o for o in observations if o.resource_type != "ecs_server" or (
        ecs_phase_enabled
        and ((o.resource_type, o.resource_id) in known and collection_policy.resource_included(
            known[(o.resource_type, o.resource_id)], policy)
             or phase is None and policy.selection.mode == "all"
             and (o.resource_type, o.resource_id) not in known))]
    groups = defaultdict(list)
    for o in observations:
        groups[(o.resource_type, o.resource_id)].append(o)
    ordered_groups = sorted(groups.items(), key=lambda item: (item[0][1], item[0][0])) \
        if service == "gaussdb" else groups.items()
    resources = {k: upsert_resource(db, service, g, now, ctx, known) for k, g in ordered_groups}
    db.flush()
    _save_dcs_memory_samples(db, groups, resources, ctx, now)
    if service == "dcs":
        from app.services.dcs_monitoring import save_metric_samples

        save_metric_samples(db, groups, resources, ctx, now)
    if service == "gaussdb":
        from app.services.gaussdb_history import persist_disk_samples

        persist_disk_samples(db, groups, resources, ctx, now)
    rules = rules_by_target(db, {k[0] for k in groups} | inspection_evidence.task_types(ctx))
    if ecs_target:
        from app.metrics.ecs_readings import ecs_source

        if ecs_source(ctx.tenant, policy.model_dump(mode="json"), client=ctx.client) == "MANAGEONE":
            # CLOUD_VM.diskUsage is cloud-volume usage, not filesystem usage.
            rules = {key: selected for key, selected in rules.items()
                     if key != ("ecs_server", "disk")}
    checks = [(o, rule, fingerprint(ctx.account, ctx.project_id, o.resource_type, o.resource_id, rule.id, o.sub_object))
              for o in observations for rule in rules.get((o.resource_type, o.check_item), [])
              if phase != "metrics"]
    fingerprints = list({fp for _, _, fp in checks})
    states, missing_resources, missing_counts = inspection_evidence.interrupt_missing(db, ctx, rules, checks, now)
    inspection_evidence.mark_missing_resources(missing_resources, missing_counts, ctx)
    active_by_fp: dict[str, AlarmEvent] = {}
    resolved_by_fp: dict[str, AlarmEvent] = {}
    for start in range(0, len(fingerprints), 500):
        chunk = fingerprints[start:start + 500]
        states.update((st.fingerprint, st) for st in db.scalars(select(EvalState).where(EvalState.fingerprint.in_(chunk))))
        for a in db.scalars(select(AlarmEvent).where(AlarmEvent.fingerprint.in_(chunk),
                (AlarmEvent.status.in_(ACTIVE_STATUSES)) | ((AlarmEvent.status == "RESOLVED") &
                (AlarmEvent.resolved_at >= now - alarm_service.REOPEN_WINDOW))).order_by(AlarmEvent.resolved_at.desc())):
            if a.status in ACTIVE_STATUSES:
                active_by_fp[a.fingerprint] = a
            else:
                resolved_by_fp.setdefault(a.fingerprint, a)
    latest: dict[int, datetime] = {}
    active_ids = [a.id for a in active_by_fp.values()]
    for start in range(0, len(active_ids), 500):
        latest.update({aid: at for aid, at in db.execute(
            select(Notification.alarm_id, func.max(Notification.created_at)).where(
                Notification.alarm_id.in_(active_ids[start:start + 500])).group_by(Notification.alarm_id)) if aid is not None})
    cache = alarm_service.DecisionCache(active_by_fp, resolved_by_fp, silence_service.active_silences(db, now),
                                        latest, load_routing(db))
    events: list[dict] = []
    evaluated: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for resource_id, unavailable in missing_counts.items():
        evaluated[resource_id][1] = unavailable
    extra = {"project": ctx.project_id, "region": ctx.region or None, "tenant": ctx.tenant_name or None,
             "tenant_code": ctx.account}
    for o, rule, fp in checks:
        res = resources[(o.resource_type, o.resource_id)]
        if not res.monitored or res.lifecycle != "ACTIVE":
            continue
        st = states.get(fp)
        if st is None:
            st = EvalState(fingerprint=fp, data={})
            states[fp] = st
            db.add(st)
        data = dict(st.data or {})
        last_evaluated = collection_policy._at(data.get("_last_evaluated_at"))
        stale_after = ctx.params.get("_inspection_stale_after_sec", 900)
        if last_evaluated is not None and (now - last_evaluated).total_seconds() > stale_after:
            # 采集中断后不能把前后两段样本凑成“连续命中/恢复”。
            data["hits"] = data["ok_hits"] = 0
        if o.resource_type == "ecs_server" and o.check_item in ("cpu", "memory", "disk"):
            if not policy.metrics.enabled:
                continue
            metric_key = {"cpu": "cpu_util", "memory": "mem_util", "disk": "disk_util"}[o.check_item]
            if metric_key not in policy.metric_keys:
                continue
            signature = o.metric_config_signature or o.facts.get("metric_config_signature")
            if isinstance(signature, str) and signature and data.get("_ecs_metric_signature") != signature:
                # 来源或映射切换后重新累计条件；不把两种口径凑成连续超限。
                data = {"_ecs_metric_signature": signature}
            observed_at = o.metric_observed_at.isoformat() if o.metric_observed_at else o.facts.get("metric_observed_at")
            if observed_at is not None or phase == "inspection":
                observed = collection_policy._at(observed_at)
                previous = collection_policy._at(data.get("_ecs_metric_observed_at"))
                if (observed is None or observed < now - timedelta(seconds=policy.stale_after_sec)
                        or observed > now + timedelta(seconds=30)):
                    data["hits"] = data["ok_hits"] = 0
                    st.data = data
                    evaluated[res.id][1] += 1
                    continue
                if previous is not None and observed <= previous:
                    evaluated[res.id][0] += 1
                    st.data = data
                    continue
                data["_ecs_metric_observed_at"] = observed.astimezone(UTC).isoformat()
        facts = {**inspection_evidence.evidence_facts(o, res, rule, ctx, data, now),
                 "critical": res.critical, "expected_running": res.expected_state.get("running", True)}
        active = active_by_fp.get(fp)
        if active is not None:
            active.resource_ref, active.tenant_id = res.id, res.tenant_id
        decision = evaluate_rule(rule, facts, data, active.severity if active else None)
        if decision.match is None and not inspection_evidence.state_known(o):
            data = inspection_evidence.interrupt(data, now)
            decision = Decision(None, message="状态证据缺失或未识别，保持告警现状", data_available=False)
        data["_last_evaluated_at"] = now.isoformat()
        evaluated[res.id][0 if decision.data_available else 1] += 1
        st.data = data
        if decision.action:
            ev = alarm_service.apply_decision(db, rule=rule, decision=decision, fp=fp, resource=res, obs=o,
                                              facts=facts, now=now, active=active, cache=cache,
                                              extra={k: v for k, v in extra.items() if v})
            if ev:
                events.append(ev)
    if phase != "metrics":
        for resource in resources.values():
            known_count, missing_count = evaluated[resource.id]
            inspection_evidence.record_inspection(resource, ctx, known_count, missing_count, now,
                observations=groups[(resource.resource_type, resource.resource_id)])
    db.flush()
    refresh_health(db, list({**missing_resources, **{r.id: r for r in resources.values()}}.values()), now)
    db.commit()
    return events, sum(1 for r in resources.values() if r.health not in ("OK", "UNKNOWN"))


def run_task(db: Session, task_code: str, *, client: HcsClient | None = None, target: Target | None = None,
             trigger: str = "SCHEDULE") -> InspectionRun:
    """对单个租户目标执行一次任务（target 为空时按环境变量单账号兜底）。"""
    target = target or (legacy_target() if task_code != CLEANUP_TASK else None)
    with log_context(task=task_code, task_execution_id=uuid4().hex, run_id=None,
                     tenant_id=target.tenant_id if target else None,
                     project_id=target.project_id if target else None,
                     project_ref=target.project_ref if target else None, trigger=trigger):
        try:
            phase = collection_policy.TASK_PHASES.get(task_code)
            if phase is None:
                return _run_task(db, task_code, client=client, target=target, trigger=trigger)
            target = target or legacy_target()
            policy = collection_policy.effective(db, target.tenant_id)
            claimed = collection_policy.claim(db, target.tenant_id, target.project_ref, phase, policy,
                                               force=trigger != "SCHEDULE")
            if claimed is None:
                run = InspectionRun(task_code=task_code, tenant_id=target.tenant_id, project_ref=target.project_ref,
                                    trigger_type=trigger, started_at=utcnow(), finished_at=utcnow(), status="SKIPPED",
                                    error_summary="采集阶段已关闭、尚未到期或正在执行", stats={"phase": phase})
                db.add(run)
                db.commit()
                return run
            try:
                with collection_policy.maintain_claim(db, claimed):
                    run = _run_task(db, task_code, client=client, target=target, trigger=trigger)
            except Exception:
                db.rollback()
                collection_policy.finish(db, claimed, False)
                raise
            collection_policy.finish(db, claimed, run.status == "SUCCESS")
            return run
        except Exception as exc:
            _log_failure("task_unexpected_failure", exc)
            raise


def _run_task(db: Session, task_code: str, *, client: HcsClient | None, target: Target | None,
              trigger: str) -> InspectionRun:
    started = perf_counter()
    if task_code == CLEANUP_TASK:                  # 数据清理：全局执行一次，不区分租户
        return run_cleanup_task(db, trigger=trigger)
    target = target or legacy_target()
    if task_code == LEDGER_TASK:                   # 台账同步与巡检共用任务调度、运行记录
        log.info("task_started", extra={"event": "task_started", "cloud_service": "ledger"})
        result = sync_ledger(db, client=client, target=target, trigger=trigger)
        _log_run(result, started, cloud_service="ledger")
        return result
    collector = TASKS.get(task_code)
    if collector is None:
        raise BizError(ErrorCode.NOT_FOUND, f"未实现的巡检任务：{task_code}", 404)
    task = db.get(InspectionTask, task_code)
    run = InspectionRun(task_code=task_code, tenant_id=target.tenant_id, project_ref=target.project_ref,
                        trigger_type=trigger, started_at=utcnow(),
                        status="RUNNING")
    db.add(run)
    db.commit()
    bind_log_context(run_id=run.id, cloud_service=collector.service)
    phase = collection_policy.TASK_PHASES.get(task_code)
    policy = collection_policy.effective(db, target.tenant_id) if phase else None
    if (not collect_switch.load(db).task_enabled(target.tenant_id, task_code)
            or collector.service == "gaussdb" and task is not None and not task.enabled
            or task_code == cce_settings.TASK_CODE and (
                not cce_settings.effective(db, target.tenant_id).enabled or not task or not task.enabled)
            or phase is not None and not collection_policy.stage_enabled(db, target.tenant_id, phase, policy)):
        _interrupt_task(db, target, task_code, utcnow())
        run.status, run.finished_at = "SKIPPED", utcnow()
        run.error_summary = "已关闭采集"
        run.stats = {"skipped": collect_switch.TASK_TYPES.get(task_code, [])}
        db.commit()
        _log_run(run, started, reason="collection_disabled")
        return run
    log.info("task_started", extra={"event": "task_started"})
    events: list[dict] = []
    client_calls = 0
    calls_before = 0
    stats_before: dict = {}
    observations: list[Observation] = []
    error_count = 0
    try:
        client = client or target.client()
        calls_before, stats_before = client.calls, client.metrics_snapshot()
        collection = settings_service.get_collect(db)
        client.configure_qps(collection.hcs_qps)
        ctx = RunContext(client=client, account=target.code, project_id=target.project_id,
                         workspaces=list(target.workspaces), params={"hcs_concurrency": collection.hcs_concurrency,
                                                                    **((task.params if task else {}) or {})},
                         tenant_id=target.tenant_id, tenant_name=target.short_name, project_ref=target.project_ref,
                         region=target.region, db=db, tenant=target.tenant)
        ctx.params.update(_task_code=task_code)
        _inspection_task_scope(db, ctx)
        _sync_scope(db, ctx, target, task_code)
        ctx.params["_inspection_stale_after_sec"] = inspection_evidence.inspection_ttl(db, ctx, task, policy, utcnow())
        # 任何资源任务都不传递旧 AOM 映射；不依赖各采集器自行判断。
        ctx.params.pop("aom", None)
        if policy is not None:
            ctx.params.update(_collection_policy=policy.model_dump(mode="json"), _collection_phase=phase)
            # A saved tenant null explicitly removes a legacy global mapping.
            ctx.params.pop("aom", None)
            ctx.params.pop("prometheus", None)
            if policy.prometheus is not None:
                ctx.params["prometheus"] = dict(policy.prometheus.queries)
        observations = collector.collect(ctx)
        if collector.service == "gaussdb":
            _inspection_task_scope(db, ctx)
            if "_inspection_tasks" in ctx.params and task_code not in ctx.params["_inspection_tasks"]:
                raise _CollectionPolicyChanged()
        current_scopes = collect_scope.load(db)
        if ctx.scopes is not None and any(
                ctx.scopes.rules(target.tenant_id, rtype) != current_scopes.rules(target.tenant_id, rtype)
                for rtype in collect_switch.TASK_TYPES.get(task_code, [])):
            # CCE 健康快照不经过 process_observations，也必须丢弃范围变更前的未提交结果。
            raise _CollectionPolicyChanged()
        if task_code == cce_settings.TASK_CODE and (
                not collect_switch.load(db).enabled(target.tenant_id, "cce_cluster")
                or cce_settings.effective(db, target.tenant_id).model_dump() != ctx.params.get("_cce_settings")):
            raise _CollectionPolicyChanged()
        if policy is not None and (collection_policy.effective(db, target.tenant_id) != policy
                or not collection_policy.stage_enabled(db, target.tenant_id, phase or "inspection", policy)):
            raise _CollectionPolicyChanged()
        events, abnormal = (([], 0) if task_code == cce_settings.TASK_CODE else
                            process_observations(db, collector.service, observations, ctx, now=utcnow()))
        run.resource_total = (int(ctx.stats.get("cce_health", {}).get("clusters", 0))
                              if task_code == cce_settings.TASK_CODE else
                              len({(o.resource_type, o.resource_id) for o in observations})
                              - int(ctx.stats.get("out_of_scope", 0)))
        run.abnormal_total = abnormal
        run.status = "PARTIAL" if ctx.errors else "SUCCESS"
        run.error_summary = "\n".join(ctx.errors[:20]) or None
        run.stats = dict(ctx.stats)
        error_count = len(ctx.errors)
    except _CollectionPolicyChanged:
        db.rollback()
        _interrupt_task(db, target, task_code, utcnow())
        run.status, run.error_summary = "SKIPPED", "采集期间策略或开关已变更，已丢弃本次未提交采样"
        run.stats = {"phase": phase, "reason": "policy_changed"}
    except HcsError as e:
        _log_failure("task_collection_failed", e, upstream_status=e.status, error_code=e.code)
        error_count = 1
        db.rollback()
        _interrupt_task(db, target, task_code, utcnow())
        run.status, run.error_summary = "FAILED", str(e)
    except Exception as e:  # noqa: BLE001
        _log_failure("task_processing_failed", e)
        error_count = 1
        db.rollback()
        _interrupt_task(db, target, task_code, utcnow())
        run.status, run.error_summary = "FAILED", f"{type(e).__name__}: {e}"
    finally:
        client_calls = max(0, client.calls - calls_before) if client is not None else 0
        if client is not None:
            run.stats = {**(run.stats or {}), "hcs": metrics_delta(client.metrics_snapshot(), stats_before)}
    run.finished_at, run.api_calls = utcnow(), client_calls
    db.commit()
    _log_run(run, started, observation_count=len(observations), event_count=len(events), error_count=error_count)
    platform_watch.on_run(db, run)
    for ev in events:
        publish(ev)
    return run


def run_cleanup_task(db: Session, trigger: str = "SCHEDULE") -> InspectionRun:
    with log_context(task=CLEANUP_TASK, task_execution_id=uuid4().hex, run_id=None,
                     tenant_id=None, project_id=None, project_ref=None, trigger=trigger):
        return _run_cleanup_task(db, trigger)


def _run_cleanup_task(db: Session, trigger: str) -> InspectionRun:
    started = perf_counter()
    run = InspectionRun(task_code=CLEANUP_TASK, tenant_id=None, trigger_type=trigger, started_at=utcnow(),
                        status="RUNNING", stats={})
    db.add(run)
    db.commit()
    bind_log_context(run_id=run.id)
    log.info("task_started", extra={"event": "task_started"})
    try:
        deleted = run_cleanup(db)
        run.status, run.stats = "SUCCESS", deleted
        run.resource_total = sum(deleted.values())
    except Exception as e:  # noqa: BLE001
        _log_failure("cleanup_failed", e)
        db.rollback()
        run.status, run.error_summary = "FAILED", f"{type(e).__name__}: {e}"[:500]
    run.finished_at = utcnow()
    db.commit()
    _log_run(run, started, deleted_counts=run.stats or {})
    platform_watch.on_run(db, run)
    return run


def skipped_targets(db: Session, task_code: str, tenant_ids: list[int] | None = None) -> list[Target]:
    """因采集开关关闭而跳过的租户目标（立即执行时提示用户）。"""
    sw = collect_switch.load(db)
    phase = collection_policy.TASK_PHASES.get(task_code)
    return [t for t in resolve_targets(db, tenant_ids) if not sw.task_enabled(t.tenant_id, task_code)
            or task_code == cce_settings.TASK_CODE and not cce_settings.status(db, t.tenant_id or 0)["effective_enabled"]
            or phase is not None and not collection_policy.stage_enabled(db, t.tenant_id, phase)]


def run_all(db: Session, task_code: str, *, trigger: str = "SCHEDULE", tenant_ids: list[int] | None = None,
            http: httpx.Client | None = None, mock_url: str | None = None) -> list[InspectionRun]:
    """按租户逐个执行（某个租户失败不影响其他租户）；采集开关关闭的租户跳过，不产生运行记录。"""
    if task_code == CLEANUP_TASK:
        return [run_cleanup_task(db, trigger=trigger)]
    runs = []
    sw = collect_switch.load(db)
    for t in resolve_targets(db, tenant_ids):
        if task_code == cce_settings.TASK_CODE and not cce_settings.status(db, t.tenant_id or 0)["effective_enabled"]:
            continue
        if not sw.task_enabled(t.tenant_id, task_code):
            log.debug("task_target_skipped", extra={"event": "task_target_skipped", "task": task_code,
                                                   "tenant_id": t.tenant_id, "project_id": t.project_id,
                                                   "reason": "collection_disabled"})
            continue
        phase = collection_policy.TASK_PHASES.get(task_code)
        if phase is not None and (not collection_policy.stage_enabled(db, t.tenant_id, phase)
                or trigger == "SCHEDULE" and not collection_policy.due(db, t.tenant_id, t.project_ref, phase)):
            continue
        started = perf_counter()
        try:
            client = t.client(http=http, mock_url=mock_url)
        except HcsError as e:            # 例如租户未配置 AK/SK：记一条失败的运行记录
            _interrupt_task(db, t, task_code, utcnow())
            run = InspectionRun(task_code=task_code, tenant_id=t.tenant_id, project_ref=t.project_ref,
                                trigger_type=trigger, started_at=utcnow(),
                                finished_at=utcnow(), status="FAILED", error_summary=str(e), stats={})
            db.add(run)
            db.commit()
            with log_context(task=task_code, task_execution_id=uuid4().hex,
                             run_id=run.id, tenant_id=t.tenant_id, project_id=t.project_id,
                             project_ref=t.project_ref, trigger=trigger):
                _log_failure("task_client_initialization_failed", e, upstream_status=e.status, error_code=e.code)
                _log_run(run, started, error_count=1)
            platform_watch.on_run(db, run)
            runs.append(run)
            continue
        runs.append(run_task(db, task_code, client=client, target=t, trigger=trigger))
    return runs
