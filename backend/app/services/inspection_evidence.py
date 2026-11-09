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
from app.collectors.gaussdb import NATIVE_STATES
from app.collectors.mrs import CLUSTER_STATES, NODE_STATES
from app.collectors.mrs_native import native_state_known
from app.core.config import get_settings
from app.metrics.dcs_samples import parse_observed_at
from app.models import AlarmEvent, EvalState, InspectionTask, Resource
from app.rules.model import Rule
from app.schemas.collection_policy import CollectionPolicy

from . import collect_switch, collection_policy
from .catalog import TASK_CHECKS

# TASK_CHECKS（任务 → 负责的检查项）由 catalog.TASK_SPECS 派生，只用于无归属元数据的老状态恢复。
# 未知/自定义任务不猜测其他任务的归属。
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


def _gauss_tasks() -> set[str]:
    return {code for code, kinds in collect_switch.TASK_TYPES.items()
            if "gaussdb_instance" in kinds and TASK_CHECKS.get(code)}


def _configured_gauss_tasks(db: Session) -> list[str] | None:
    configured = db.execute(select(InspectionTask.code, InspectionTask.enabled).where(
        InspectionTask.code.in_(_gauss_tasks()))).all()
    # 没有配置行是老调用/纯观测测试；[] 则是已经配置但全部停用，二者不可混用。
    return sorted(code for code, enabled in configured if enabled) if configured else None


def _owns_state(ctx: Context, data: dict, enabled: dict[str, Rule], fallback: Rule | None = None) -> bool:
    """旧混合任务的 scope 不能凌驾于当前检查项归属。"""
    meta = data.get(META)
    meta = meta if isinstance(meta, dict) else {}
    code = meta.get("rule_id")
    rule = enabled.get(code) if isinstance(code, str) else fallback
    kind = meta.get("resource_type") or (rule.resource_type if rule else None)
    if kind != "gaussdb_instance" and "gaussdb_instance" not in task_types(ctx):
        return True
    check = meta.get("check_item") or (rule.check_item if rule else None)
    allowed = TASK_CHECKS.get(task_code(ctx) or "", set())
    return check in allowed and (rule is None or rule.check_item in allowed)


def _task_results(raw: dict) -> dict[str, dict]:
    tasks = raw.get("tasks")
    if isinstance(tasks, dict):
        return {code: dict(result) for code, result in tasks.items()
                if code in _gauss_tasks() and isinstance(result, dict) and result.get("task_code") == code}
    code = raw.get("task_code")
    # 老混合结果只能保留在原 task 名下；绝不复制时间给尚未采样的新任务。
    return {code: {key: value for key, value in raw.items() if key not in ("tasks", "required_tasks")}} \
        if isinstance(code, str) and code in _gauss_tasks() else {}


def _gauss_observation_available(observation: Observation) -> bool:
    if observation.check_item == "instance_status":
        status = observation.facts.get("status")
        return isinstance(status, str) and status in NATIVE_STATES | {"FROZEN"}
    if observation.check_item == "disk":
        usage = observation.facts.get("disk_usage")
        return (isinstance(usage, int | float) and not isinstance(usage, bool)
                and 0 <= usage <= 100 and math.isfinite(usage))
    if observation.check_item == "components":
        down, unknown = observation.facts.get("down_components"), observation.facts.get("unknown_components")
        return type(down) is int and down >= 0 and type(unknown) is int and unknown == 0
    return False


def record_inspection(resource: Resource, ctx: Context, checked_rules: int, unavailable_rules: int,
                      evaluated_at: datetime | None, *, observations: list[Observation] | None = None) -> None:
    """原地更新 attrs，不提交。None 表示缺采，只保留当前任务自己的上次评估时间。

    Gauss 的 _inspection_tasks 是当前启用任务的完整列表（空列表也有意义）；
    _task_code 标识本轮归属，_inspection_stale_after_sec 只适用于该任务。
    observations 为本资源的本轮观测；零适用规则时，仍须验证真实采样才允许 FRESH。
    """
    raw = (resource.attrs or {}).get("inspection_monitoring")
    raw = raw if isinstance(raw, dict) else {}
    code = task_code(ctx)
    gauss = resource.resource_type == "gaussdb_instance" and code is not None
    if resource.resource_type == "gaussdb_instance" and code is None and "tasks" in raw:
        return  # 无显式任务的老调用不能覆盖已经拆分的结果。
    if gauss and (resource.tenant_id != ctx.tenant_id or resource.project_ref != ctx.project_ref):
        raise ValueError("Gauss 巡检上下文与资源的租户/项目不匹配")
    tasks = _task_results(raw) if gauss else {}
    previous = tasks.get(code or "", {}) if gauss else raw
    result = {"evaluated_at": evaluated_at.isoformat() if evaluated_at is not None else previous.get("evaluated_at"),
              "checked_rules": checked_rules, "unavailable_rules": unavailable_rules,
              "stale_after_sec": ctx.params.get("_inspection_stale_after_sec", previous.get("stale_after_sec", 900)
                                                if evaluated_at is None else 900),
              "task_code": code}
    if not gauss:
        resource.attrs = {**(resource.attrs or {}), "inspection_monitoring": result}
        return
    if checked_rules == unavailable_rules == 0 and evaluated_at is not None:
        result["no_applicable_rules"] = True
        result["observation_available"] = any(
            item.resource_type == resource.resource_type and item.resource_id == resource.resource_id
            and item.check_item in TASK_CHECKS.get(code or "", set()) and _gauss_observation_available(item)
            for item in observations or [])
    required = ctx.params.get("_inspection_tasks", raw.get("required_tasks"))
    if not isinstance(required, list):
        required = [code] if code in _gauss_tasks() else []
    required = sorted({task for task in required if isinstance(task, str) and task in _gauss_tasks()})
    if code in _gauss_tasks():
        tasks[code] = {**result, SCOPE: scope_key(ctx)}
    selected = [tasks.get(task, {}) for task in required]
    times = [parse_observed_at(item.get("evaluated_at")) for item in selected]
    valid_times = [at for at in times if at is not None]
    checked = sum(item.get("checked_rules", 0) for item in selected if type(item.get("checked_rules")) is int
                  and item["checked_rules"] > 0)
    unavailable = sum(item.get("unavailable_rules", 0) for item in selected
                      if type(item.get("unavailable_rules")) is int and item["unavailable_rules"] > 0)
    unavailable += sum(not item or type(item.get("checked_rules")) is not int for item in selected)
    thresholds = [item["stale_after_sec"] for item in selected
                  if type(item.get("stale_after_sec")) is int and item["stale_after_sec"] >= 60]
    # 扁平摘要供既有展示读取；健康判定始终逐任务计算有效期。
    summary = {**result, "version": 2, "tasks": tasks, "required_tasks": required,
               "evaluated_at": min(valid_times).isoformat() if valid_times and len(valid_times) == len(times) else None,
               "checked_rules": checked, "unavailable_rules": unavailable,
               "stale_after_sec": min(thresholds) if thresholds else result["stale_after_sec"]}
    resource.attrs = {**(resource.attrs or {}), "inspection_monitoring": summary}


def sync_gauss_task_requirements(db: Session) -> int:
    """任务开关为全局配置；仅同步 Gauss 的需求元数据，不伪造采样或恢复告警。"""
    db.flush()
    # 先串行化资源更新，再读当前配置；等待锁之前的任务快照可能已经过时。
    resources = list(db.scalars(select(Resource).where(Resource.resource_type == "gaussdb_instance")
        .order_by(Resource.resource_id, Resource.resource_type, Resource.id).with_for_update()
        .execution_options(populate_existing=True)))
    required = _configured_gauss_tasks(db) or []
    changed = 0
    for resource in resources:
        raw = (resource.attrs or {}).get("inspection_monitoring")
        raw = raw if isinstance(raw, dict) else {}
        if raw.get("required_tasks") == required and isinstance(raw.get("tasks"), dict):
            continue
        resource.attrs = {**(resource.attrs or {}), "inspection_monitoring": {
            **raw, "version": 2, "tasks": _task_results(raw), "required_tasks": required}}
        changed += 1
    return changed


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


# 按实例汇总的子对象状态：有未识别状态时只允许触发，不允许恢复（未知不等于正常）
UNKNOWN_COUNTS = {("dcs_instance", "nodes"): "unknown_nodes", ("gaussdb_instance", "components"): "unknown_components"}


def state_known(observation: Observation) -> bool:
    if observation.resource_type == "mrs_job" and observation.attrs.get("source") != "dataarts_instance_detail":
        return native_state_known(observation.facts)
    # DataArts/集群状态枚举来自上传 PDF：DataArts 表 5-178 / 5-182、MRS 集群状态表；
    # DCS 节点状态见 DCS API 表 4-109，GaussDB 组件状态见 GaussDB API 表 4-80（由采集器统计未识别数）。
    unknown = UNKNOWN_COUNTS.get((observation.resource_type, observation.check_item))
    if unknown is not None:
        return observation.facts.get(unknown) == 0
    checks = {
        ("dgc_realtime_job", "job_status"): ("status", REALTIME_STATES),
        ("dgc_realtime_job", "node_status"): ("node_status", REALTIME_STATES),
        ("dgc_batch_job", "instance_status"): ("last_status", BATCH_STATES),
        ("mrs_cluster", "cluster_status"): ("cluster_state", CLUSTER_STATES),
        ("mrs_job", "job_status"): ("node_status", NODE_STATES),  # 仅旧来源快照兼容
        ("dgc_mrs_node", "job_status"): ("node_status", NODE_STATES),
    }
    contract = checks.get((observation.resource_type, observation.check_item))
    if contract is None:
        return True
    key, states = contract
    value = observation.facts.get(key)
    return isinstance(value, str) and value in states


# 一次执行的标识：标识变化即新一轮执行，状态持续时间与连续命中重新计算（流水线 upsert 共用）
EXECUTION_ID_FIELDS = {"mrs_job": "job_id", "dgc_mrs_node": "instance_id", "dgc_batch_job": "instance_id"}


def execution_id(observation: Observation) -> str | None:
    field = EXECUTION_ID_FIELDS.get(observation.resource_type)
    if observation.resource_type == "mrs_job" and observation.attrs.get("source") == "dataarts_instance_detail":
        field = "instance_id"
    value = observation.attrs.get(field) if field else None
    return str(value) if type(value) is int or isinstance(value, str) and value.strip() else None


def _state(observation: Observation) -> Any:
    keys = (("node_status",) if observation.sub_object else
            ("last_status", "status", "cluster_state", "node_status", "job_state", "job_result"))
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
               or prior.get("task_code") != task_code(ctx) or prior.get("check_item") != observation.check_item
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
    elif state is not None and (observation.resource_type not in EXECUTION_ID_FIELDS or execution is not None):
        facts["status_duration_min"] = int((now - since).total_seconds() // 60)
    return facts


def interrupt_missing(db: Session, ctx: Context, rules: dict[tuple[str, str], list[Rule]],
                      checks: list[tuple[Observation, Rule, str]], now: datetime
                      ) -> tuple[dict[str, EvalState], dict[int, Resource], dict[int, int]]:
    """只清当前 task/tenant/project 的未返回指纹；旧库不可归属的孤儿留至首次有效观测重建。"""
    owner = scope_key(ctx)
    if owner is None or ctx.params.get("_collection_phase") == "metrics":
        return {}, {}, {}
    gauss = "gaussdb_instance" in task_types(ctx)
    current = {fp for _, _, fp in checks}
    configured = ctx.params.get("_collection_policy") or {}
    policy = CollectionPolicy.model_validate(configured)
    enabled = {rule.id: rule for (kind, check), group in rules.items() for rule in group
               if not (kind == "ecs_server" and check in collection_policy.METRIC_CHECKS
                       and (not policy.metrics.enabled or collection_policy.METRIC_CHECKS[check] not in policy.metric_keys))}
    bound: dict[str, tuple[Resource, Rule, str]] = {}
    resource_query = select(Resource).where(
        Resource.tenant_id == ctx.tenant_id, Resource.project_ref == ctx.project_ref,
        Resource.resource_type.in_(task_types(ctx)))
    if gauss:
        # 与成功观测的资源行锁串行，失败处理不能用旧 attrs 覆盖其他任务的新结果。
        db.flush()
        resource_query = resource_query.order_by(Resource.resource_id, Resource.resource_type, Resource.id) \
            .with_for_update().execution_options(populate_existing=True)
    resources = list(db.scalars(resource_query))
    if gauss:
        # failure ctx 也可能在等待资源锁期间遇到 TaskPatch，不能继续使用锁前快照。
        configured = _configured_gauss_tasks(db)
        if configured is not None:
            ctx.params["_inspection_tasks"] = configured
        required = ctx.params.get("_inspection_tasks")
        if isinstance(required, list) and task_code(ctx) not in required:
            return {}, {}, {}  # 停用任务不再中断证据；告警仍等待真实观测恢复。
    items = list(db.scalars(select(EvalState).where(EvalState.data[SCOPE].as_string() == owner)
                           .order_by(EvalState.fingerprint).with_for_update()))
    items = [state for state in items if _owns_state(ctx, state.data or {}, enabled)]
    if "ecs_server" in task_types(ctx):
        included_refs = {resource.id for resource in resources
                         if resource.resource_type == "ecs_server"
                         and collection_policy.resource_included(resource, policy)}
        # Only selected resources belong to this inspection; an omitted ECS
        # instance must not have its pending rule state interrupted here.
        items = [state for state in items if ((state.data or {}).get(META) or {}).get("resource_ref") in included_refs]
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
    # 采集范围之外的资源本来就不会被采集，不能因为“本轮没返回”而被记为缺采
    resources = [resource for resource in resources if not resource.scope_excluded and (
        resource.resource_type != "ecs_server" or collection_policy.resource_included(resource, policy)) and (
        resource.id in owned_refs or (resource.resource_type, resource.resource_id) in current_ids
        or gauss
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
            # 也保留其他 scope 的存在性，不能因未接管而创建同指纹的重复主键。
            states[state.fingerprint] = state
    observed_resources = {(observation.resource_type, observation.resource_id) for observation, _, _ in checks}
    attributable = {binding[0].id for fp, binding in bound.items() if fp in states
                    and (states[fp].data or {}).get(SCOPE) in (None, owner)
                    and _owns_state(ctx, states[fp].data or {}, enabled, binding[1])}
    if gauss and ctx.project_ref is not None:
        attributable.update(resource.id for resource in resources)
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
        if pending is not None and ((pending.data or {}).get(SCOPE) not in (None, owner)
                                    or not _owns_state(ctx, pending.data or {}, enabled, rule)):
            if gauss and resource.id in attributable:
                missing[resource.id] = resource
                counts[resource.id] += 1
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
        record_inspection(resource, ctx, 0, counts[resource.id], None)
