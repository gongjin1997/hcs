"""规则自身的时间与采集归属；缺采仅中断证据，不作告警恢复判定。"""
import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alarms.fingerprint import fingerprint
from app.collectors.base import Observation, RunContext
from app.collectors.dataarts import BATCH_STATES, REALTIME_STATES
from app.collectors.mrs import CLUSTER_STATES
from app.core.config import get_settings
from app.metrics.dcs_samples import parse_observed_at
from app.models import AlarmEvent, EvalState, InspectionTask, Resource
from app.rules.model import Rule
from app.schemas.collection_policy import CollectionPolicy

from . import collect_switch, collection_policy

# 只用于无归属元数据的老状态恢复。未知/自定义任务不猜测其他任务的归属。
TASK_CHECKS = {
    "dgc_realtime_status": {"job_status", "node_status"}, "dgc_batch_instance": {"instance_status"},
    "mrs_cluster_status": {"cluster_status"}, "mrs_job_status": {"job_status"},
    "gaussdb_status": {"instance_status", "cpu", "memory", "disk"},
    "dcs_status": {"instance_status", "memory"},
    "ecs_status": {"instance_status", "cpu", "memory", "disk", "reboot"},
    "ecs_metrics": set(), "ecs_quota": set(),
    "cce_pod_events": {"pod_restart", "pod_oom", "pod_crashloop", "pod_backpressure", "pod_evicted"},
}
META = "_inspection_evidence"
SCOPE = "_evidence_scope"


@dataclass
class ScopeContext:
    account: str
    project_id: str
    tenant_id: int | None
    project_ref: int | None
    params: dict


Context = RunContext | ScopeContext


@lru_cache(maxsize=64)
def runtime_metric_keys(resource_type: str) -> frozenset[str]:
    if resource_type in ("ecs_server", "dcs_instance", "gaussdb_instance"):
        return frozenset()
    from .resource_guides import get_resource_guide

    guide = get_resource_guide(resource_type)
    return frozenset(field.key for field in guide.metrics if field.availability != "not_collected") if guide else frozenset()


def task_code(ctx: Context) -> str | None:
    code = ctx.params.get("_task_code")
    return code if isinstance(code, str) and code else None


def scope_key(ctx: Context) -> str | None:
    code = task_code(ctx)
    if code is None:
        return None
    value = [ctx.tenant_id, ctx.project_ref, code,
             ctx.account if ctx.tenant_id is None else None, ctx.project_id if ctx.project_ref is None else None]
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def task_types(ctx: Context) -> set[str]:
    return set(collect_switch.TASK_TYPES.get(task_code(ctx) or "", ()))


def inspection_ttl(db: Session, ctx: Context, task: InspectionTask | None,
                   policy: CollectionPolicy | None, now: datetime) -> int:
    interval, cron = (task.interval_sec if task else 300), (task.cron if task else None)
    phase = collection_policy.TASK_PHASES.get(task_code(ctx) or "")
    if phase is not None and policy is not None:
        interval, cron, _source = collection_policy.schedule(db, ctx.tenant_id, phase, policy)
    if cron:
        trigger = CronTrigger.from_crontab(cron, timezone=get_settings().timezone)
        fires = []
        cursor = now
        previous = None
        # 用完整调度间隔，不能把手工执行恰好临近下一次触发误算为很短的周期。
        for _ in range(4):
            following = trigger.get_next_fire_time(previous, cursor)
            if following is None:
                raise ValueError("巡检 cron 没有未来执行时刻")
            fires.append(following.astimezone(UTC))
            previous, cursor = following, following + timedelta(microseconds=1)
        return max(900, math.ceil((fires[-1] - fires[0]).total_seconds()))
    return max(900, interval * 3)


def state_known(observation: Observation) -> bool:
    # MRS 作业沿用现有兼容适配中已识别的文本，不把它声明为上传 PDF 的正式契约。
    checks = {
        ("dgc_realtime_job", "job_status"): ("status", REALTIME_STATES),
        ("dgc_realtime_job", "node_status"): ("node_status", REALTIME_STATES),
        ("dgc_batch_job", "instance_status"): ("last_status", BATCH_STATES),
        ("mrs_cluster", "cluster_status"): ("cluster_state", CLUSTER_STATES),
        ("mrs_job", "job_status"): ("job_state", frozenset(
            "NEW SUBMITTED ACCEPTED RUNNING FINISHED FAILED KILLED KILLING WAITING QUEUED PENDING "
            "STARTING STOPPING STOPPED CANCELED CANCELLED SUCCEEDED SUCCESS".split())),
    }
    contract = checks.get((observation.resource_type, observation.check_item))
    if contract is None:
        return True
    key, states = contract
    value = observation.facts.get(key)
    return isinstance(value, str) and value in states


def execution_id(observation: Observation) -> str | None:
    field = {"mrs_job": "job_id", "dgc_batch_job": "instance_id"}.get(observation.resource_type)
    value = observation.attrs.get(field) if field else None
    return str(value) if type(value) is int or isinstance(value, str) and value.strip() else None


def _state(observation: Observation) -> Any:
    keys = (("node_status",) if observation.sub_object else
            ("job_state", "last_status", "status", "cluster_state", "node_status"))
    return next((observation.facts[key] for key in keys if observation.facts.get(key) is not None), None)


def interrupt(data: dict, now: datetime) -> dict:
    result = {**data, "hits": 0, "ok_hits": 0}
    result[META] = {**(data.get(META) or {}), "since": None, "missing_at": now.isoformat()}
    return result


def evidence_facts(observation: Observation, resource: Resource, rule: Rule, ctx: Context,
                   data: dict, now: datetime) -> dict:
    prior = data.get(META)
    prior = prior if isinstance(prior, dict) else {}
    state, execution = _state(observation), execution_id(observation)
    since = parse_observed_at(prior.get("since"))
    previous = parse_observed_at(data.get("_last_evaluated_at"))
    stale = ctx.params.get("_inspection_stale_after_sec", 900)
    changed = (prior.get("version") != 1 or data.get(SCOPE) != scope_key(ctx)
               or prior.get("execution_id") != execution
               or since is None or since > now or previous is not None
               and (previous > now or (now - previous).total_seconds() > stale))
    if changed:
        data["hits"] = data["ok_hits"] = 0
    if changed or prior.get("state") != state:
        since = now
    since = since or now
    data[SCOPE] = scope_key(ctx)
    data[META] = {"version": 1, "resource_ref": resource.id, "resource_type": observation.resource_type,
                  "resource_id": observation.resource_id, "rule_id": rule.id, "check_item": observation.check_item,
                  "sub_object": observation.sub_object, "task_code": task_code(ctx), "state": state,
                  "execution_id": execution, "since": since.isoformat()}
    facts = dict(observation.facts)
    if "status_duration_min" in facts:
        value = facts["status_duration_min"]
        if type(value) not in (int, float) or value < 0 or isinstance(value, float) and not math.isfinite(value):
            facts.pop("status_duration_min")
    elif state is not None and (observation.resource_type != "mrs_job" or execution is not None):
        facts["status_duration_min"] = int((now - since).total_seconds() // 60)
    return facts


def interrupt_missing(db: Session, ctx: Context, rules: dict[tuple[str, str], list[Rule]],
                      checks: list[tuple[Observation, Rule, str]], now: datetime
                      ) -> tuple[dict[str, EvalState], dict[int, Resource], dict[int, int]]:
    """只清当前 task/tenant/project 的未返回指纹；旧库不可归属的孤儿留至首次有效观测重建。"""
    owner = scope_key(ctx)
    if owner is None or ctx.params.get("_collection_phase") == "metrics":
        return {}, {}, {}
    current = {fp for _, _, fp in checks}
    configured = ctx.params.get("_collection_policy") or {}
    policy = CollectionPolicy.model_validate(configured)
    enabled = {rule.id: rule for (kind, check), group in rules.items() for rule in group
               if not (kind == "ecs_server" and check in collection_policy.METRIC_CHECKS
                       and (not policy.metrics.enabled or collection_policy.METRIC_CHECKS[check] not in policy.metric_keys))}
    bound: dict[str, tuple[Resource, Rule, str]] = {}
    resources = list(db.scalars(select(Resource).where(
        Resource.tenant_id == ctx.tenant_id, Resource.project_ref == ctx.project_ref,
        Resource.resource_type.in_(task_types(ctx)))))
    items = list(db.scalars(select(EvalState).where(EvalState.data[SCOPE].as_string() == owner)
                           .order_by(EvalState.fingerprint).with_for_update()))
    states = {state.fingerprint: state for state in items}
    for state in items:
        if ((state.data or {}).get(META) or {}).get("rule_id") not in enabled:
            state.data = interrupt(dict(state.data or {}), now)
    owned_refs = {(item.data.get(META) or {}).get("resource_ref") for item in items}
    owned_refs -= {r.id for r in resources} | {None}
    if owned_refs:
        resources.extend(db.scalars(select(Resource).where(
            Resource.tenant_id == ctx.tenant_id, Resource.project_ref == ctx.project_ref,
            Resource.id.in_(owned_refs))))
    current_ids = {(observation.resource_type, observation.resource_id) for observation, _, _ in checks}
    owned_refs = {(item.data.get(META) or {}).get("resource_ref") for item in items}
    resources = [resource for resource in resources if (
        resource.resource_type != "ecs_server" or collection_policy.resource_included(resource, policy)) and (
        resource.id in owned_refs or (resource.resource_type, resource.resource_id) in current_ids
        or ((resource.attrs or {}).get("inspection_monitoring") or {}).get("task_code") in (None, task_code(ctx)))]
    by_id = {resource.id: resource for resource in resources}
    by_identity = {(r.resource_type, r.resource_id): r for r in resources}
    children: dict[tuple[int, str], set[str]] = defaultdict(set)
    for observation, rule, _fp in checks:
        resource = by_identity.get((observation.resource_type, observation.resource_id))
        if resource is not None and observation.sub_object:
            children[(resource.id, rule.id)].add(observation.sub_object)
    for start in range(0, len(resources), 500):
        for ref, rule_id, sub in db.execute(select(AlarmEvent.resource_ref, AlarmEvent.rule_id, AlarmEvent.sub_object).where(
                AlarmEvent.tenant_id == ctx.tenant_id,
                AlarmEvent.resource_ref.in_([r.id for r in resources[start:start + 500]]),
                AlarmEvent.sub_object != "")):
            if ref is not None:
                children[(ref, rule_id)].add(sub)
    for state in items:
        meta = (state.data or {}).get(META) or {}
        ref, code = meta.get("resource_ref"), meta.get("rule_id")
        if type(ref) is int and isinstance(code, str) and meta.get("sub_object"):
            children[(ref, code)].add(meta["sub_object"])
    allowed = TASK_CHECKS.get(task_code(ctx) or "")
    for resource in resources:
        for (kind, check), group in rules.items():
            if kind != resource.resource_type or allowed is not None and check not in allowed:
                continue
            if task_code(ctx) not in collect_switch.TASK_TYPES:
                continue  # 自定义任务只使用已记录的指纹归属，不能猜老库所有规则都由它负责。
            for rule in group:
                if rule.id not in enabled:
                    continue
                names = children[(resource.id, rule.id)]
                nodes = (resource.attrs or {}).get("nodes")
                if check == "node_status" and isinstance(nodes, list):
                    names.update(node["name"] for node in nodes if isinstance(node, dict)
                                 and isinstance(node.get("name"), str) and node["name"])
                for sub in names if check == "node_status" or names else ("",):
                    fp = fingerprint(ctx.account, ctx.project_id, kind, resource.resource_id, rule.id, sub)
                    bound[fp] = resource, rule, sub
    # 元数据提供旧 attrs 中已消失的子对象身份，也覆盖自定义任务自身的归属。
    for state in items:
        meta = (state.data or {}).get(META) or {}
        ref, code = meta.get("resource_ref"), meta.get("rule_id")
        if type(ref) is not int or not isinstance(code, str):
            continue
        resource, owner_rule = by_id.get(ref), enabled.get(code)
        if resource is not None and owner_rule is not None:
            bound[state.fingerprint] = resource, owner_rule, meta.get("sub_object") or ""
    candidates = sorted(set(bound) - set(states))
    for start in range(0, len(candidates), 500):
        for state in db.scalars(select(EvalState).where(EvalState.fingerprint.in_(candidates[start:start + 500]))
                                .order_by(EvalState.fingerprint).with_for_update()):
            if (state.data or {}).get(SCOPE) in (None, owner):
                states[state.fingerprint] = state
    observed_resources = {(observation.resource_type, observation.resource_id) for observation, _, _ in checks}
    attributable = {binding[0].id for fp, binding in bound.items() if fp in states}
    for item in items:
        reference = (item.data.get(META) or {}).get("resource_ref")
        if isinstance(reference, int) and not isinstance(reference, bool):
            attributable.add(reference)
    attributable.update(resource.id for resource in resources
                         if (resource.resource_type, resource.resource_id) in observed_resources)
    missing: dict[int, Resource] = {}
    counts: dict[int, int] = defaultdict(int)
    for fp, (resource, rule, sub) in bound.items():
        if fp in current:
            continue
        pending = states.get(fp)
        if pending is not None and (pending.data or {}).get(SCOPE) not in (None, owner):
            continue
        if pending is None and resource.id not in attributable:
            continue  # 老库没有该项目/任务的指纹证据时，不能凭同类型资源猜测归属。
        if pending is None:
            pending = EvalState(fingerprint=fp, data={})
            db.add(pending)
            states[fp] = pending
        data = interrupt(dict(pending.data or {}), now)
        data[SCOPE] = owner
        data[META].update(version=1, resource_ref=resource.id, resource_type=resource.resource_type,
                          resource_id=resource.resource_id, rule_id=rule.id, check_item=rule.check_item,
                          sub_object=sub, task_code=task_code(ctx))
        pending.data = data
        missing[resource.id] = resource
        counts[resource.id] += 1
    for resource in resources:
        if resource.id not in attributable:
            continue
        if (resource.attrs or {}).get("nodes") is None:
            counts[resource.id] += sum(not children[(resource.id, rule.id)]
                                       for rule in rules.get((resource.resource_type, "node_status"), ()))
            if counts[resource.id]:
                missing[resource.id] = resource
    for resource in resources:
        if resource.id in attributable and (resource.resource_type, resource.resource_id) not in observed_resources:
            missing[resource.id] = resource
            counts.setdefault(resource.id, 0)
    return states, missing, dict(counts)


def mark_missing_resources(resources: dict[int, Resource], counts: dict[int, int], ctx: Context) -> None:
    for resource in resources.values():
        previous = (resource.attrs or {}).get("inspection_monitoring") or {}
        resource.attrs = {**(resource.attrs or {}), "inspection_monitoring": {
            "evaluated_at": previous.get("evaluated_at"), "checked_rules": 0,
            "unavailable_rules": counts[resource.id],
            "stale_after_sec": ctx.params.get("_inspection_stale_after_sec", previous.get("stale_after_sec", 900)),
            "task_code": task_code(ctx)}}
