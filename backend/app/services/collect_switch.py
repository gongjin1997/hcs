"""采集开关：按资源类型（可再按租户）启停台账同步和巡检。

- 全局默认（tenant_id = 0）+ 租户单独设置（覆盖全局）；旧库缺少记录时兼容开启，新生产库显式初始化为关闭
- 关闭后：台账同步跳过该类型（已有台账保留，不会被标记为“已释放”）；对应的巡检任务对该租户不再执行；
  该类型在受影响租户下的活动告警自动关闭（留处理记录），避免留下永远不会恢复的告警
- 典型场景：租户B 没有开通 DCS —— 在租户B 下关闭“DCS 缓存实例”，DCS 巡检和台账同步都不再调用 DCS 接口
"""
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AlarmEvent, AlarmEventLog, CollectSwitch, HcsAccount, Resource
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow

from .catalog import CATEGORIES, RESOURCE_TYPES, SERVICES

GLOBAL = 0
# 巡检任务 → 采集的资源类型（任务的全部类型都关闭时，该任务对该租户跳过）
TASK_TYPES: dict[str, list[str]] = {
    "dgc_realtime_status": ["dgc_realtime_job"], "dgc_batch_instance": ["dgc_batch_job"],
    "mrs_cluster_status": ["mrs_cluster"], "mrs_job_status": ["mrs_job"], "gaussdb_status": ["gaussdb_instance"],
    "dcs_status": ["dcs_instance"], "ecs_status": ["ecs_server"], "ecs_metrics": ["ecs_server"],
    "ecs_quota": ["ecs_server"], "cce_pod_events": ["cce_workload"],
    "cce_cluster_health": ["cce_cluster"],
}
TYPE_TASKS: dict[str, list[str]] = {}
for _task, _types in TASK_TYPES.items():
    for _t in _types:
        TYPE_TASKS.setdefault(_t, []).append(_task)
SWITCH_TYPES = list(RESOURCE_TYPES)          # 14 类台账资源 + 4 类只巡检对象（作业 / 容器工作负载）


@dataclass
class Switches:
    rows: dict[tuple[int, str], bool] = field(default_factory=dict)

    def explicit(self, tenant_id: int, rtype: str) -> bool | None:
        return self.rows.get((tenant_id, rtype))

    def enabled(self, tenant_id: int | None, rtype: str) -> bool:
        if tenant_id is not None and (tenant_id, rtype) in self.rows:
            return self.rows[(tenant_id, rtype)]
        return self.rows.get((GLOBAL, rtype), True)

    def task_enabled(self, tenant_id: int | None, task_code: str) -> bool:
        types = TASK_TYPES.get(task_code)
        return True if not types else any(self.enabled(tenant_id, t) for t in types)

    def enabled_types(self, tenant_id: int | None, types: list[str]) -> list[str]:
        return [t for t in types if self.enabled(tenant_id, t)]


def load(db: Session) -> Switches:
    # 长生命周期 worker Session 不能复用开关修改前的 ORM identity-map 值。
    rows = db.execute(select(CollectSwitch.tenant_id, CollectSwitch.resource_type, CollectSwitch.enabled))
    return Switches({(tenant_id, resource_type): enabled for tenant_id, resource_type, enabled in rows})


def initialize_disabled(db: Session, tenant_id: int, operator: str) -> None:
    """新租户或新生产库逐类型关闭；重复执行不覆盖已经明确配置的开关。"""
    existing = set(db.scalars(select(CollectSwitch.resource_type).where(CollectSwitch.tenant_id == tenant_id)))
    for resource_type in SWITCH_TYPES:
        if resource_type not in existing:
            db.add(CollectSwitch(tenant_id=tenant_id, resource_type=resource_type, enabled=False, updated_by=operator))
    db.flush()
    if tenant_id > 0:
        from app.services import collection_policy

        collection_policy.initialize_disabled(db, tenant_id, operator)


def _tenants(db: Session, tenant_ids: list[int] | None) -> list[HcsAccount]:
    q = select(HcsAccount).order_by(HcsAccount.id)
    if tenant_ids is not None:
        q = q.where(HcsAccount.id.in_(tenant_ids))
    return list(db.scalars(q))


def overview(db: Session, tenant_ids: list[int] | None) -> dict:
    """采集开关页：每个资源类型的全局开关、各租户设置（None = 跟随全局）与生效状态、资源数、活动告警数。"""
    sw = load(db)
    tenants = _tenants(db, tenant_ids)
    tids = [t.id for t in tenants]
    res = dict(((tid, rt), n) for tid, rt, n in db.execute(
        select(Resource.tenant_id, Resource.resource_type, func.count()).where(
            Resource.lifecycle == "ACTIVE", Resource.tenant_id.in_(tids)).group_by(Resource.tenant_id,
                                                                                   Resource.resource_type)))
    alarms = dict(((tid, rt), n) for tid, rt, n in db.execute(
        select(AlarmEvent.tenant_id, AlarmEvent.resource_type, func.count()).where(
            AlarmEvent.status.in_(ACTIVE_STATUSES), AlarmEvent.tenant_id.in_(tids)).group_by(AlarmEvent.tenant_id,
                                                                                         AlarmEvent.resource_type)))
    types = []
    for code in SWITCH_TYPES:
        meta = RESOURCE_TYPES[code]
        types.append({
            "code": code, "name": meta["name"], "service": meta["service"],
            "service_name": SERVICES.get(meta["service"], meta["service"]), "category": meta["category"],
            "ledger": meta["ledger"], "tasks": TYPE_TASKS.get(code, []),
            "global": sw.enabled(None, code),
            "tenants": {str(t): {"setting": sw.explicit(t, code), "effective": sw.enabled(t, code),
                                 "resources": res.get((t, code), 0), "active_alarms": alarms.get((t, code), 0)}
                        for t in tids}})
    return {"categories": CATEGORIES, "types": types,
            "tenants": [{"id": t.id, "code": t.code, "name": t.name, "short_name": t.short_name or t.name,
                         "color": t.color, "enabled": t.enabled} for t in tenants]}


def update(db: Session, items: list[dict], operator: str) -> dict:
    """批量修改：items = [{resource_type, tenant_id(0=全局), enabled(True/False/None=跟随全局)}]。
    返回 {changed: [...], closed_alarms: n}。关闭导致生效状态由开 → 关时，自动关闭对应的活动告警。"""
    before = load(db)
    tenant_ids = [t.id for t in db.scalars(select(HcsAccount))]
    changed = []
    for it in items:
        rtype, tid, value = it["resource_type"], int(it.get("tenant_id") or 0), it.get("enabled")
        row = db.scalar(select(CollectSwitch).where(CollectSwitch.tenant_id == tid,
                                                    CollectSwitch.resource_type == rtype))
        old = row.enabled if row else None
        if tid != GLOBAL and value is None:          # 租户设置“跟随全局” = 删除单独设置
            if row is not None:
                db.delete(row)
        elif tid == GLOBAL and value is None:
            continue
        elif row is None:
            db.add(CollectSwitch(tenant_id=tid, resource_type=rtype, enabled=bool(value), updated_by=operator))
        else:
            row.enabled, row.updated_by = bool(value), operator
        if old != value:
            changed.append({"resource_type": rtype, "name": RESOURCE_TYPES[rtype]["name"], "tenant_id": tid,
                            "old": old, "new": value})
    db.flush()
    after = load(db)
    closed = 0
    now = utcnow()
    for rtype in {c["resource_type"] for c in changed}:
        off = [t for t in tenant_ids if before.enabled(t, rtype) and not after.enabled(t, rtype)]
        if not off:
            continue
        for a in db.scalars(select(AlarmEvent).where(AlarmEvent.resource_type == rtype,
                                                     AlarmEvent.tenant_id.in_(off),
                                                     AlarmEvent.status.in_(ACTIVE_STATUSES))):
            prev, a.status = a.status, "CLOSED"
            a.resolved_at = a.resolved_at or now
            db.add(AlarmEventLog(alarm_id=a.id, action="CLOSE", from_status=prev, to_status="CLOSED", operator=operator,
                                 note=f"已关闭“{RESOURCE_TYPES[rtype]['name']}”的采集，告警自动关闭", at=now))
            closed += 1
    return {"changed": changed, "closed_alarms": closed}


def impact(db: Session, items: list[dict]) -> dict:
    """保存前预估：本次修改会让哪些租户停止采集、关闭多少活动告警（前端确认框使用）。"""
    before = load(db)
    after = Switches(dict(before.rows))
    for it in items:
        key = (int(it.get("tenant_id") or 0), it["resource_type"])
        if it.get("enabled") is None:
            if key[0] != GLOBAL:
                after.rows.pop(key, None)
        else:
            after.rows[key] = bool(it["enabled"])
    tenants = list(db.scalars(select(HcsAccount)))
    stop, start, alarms = [], [], 0
    for rtype in {it["resource_type"] for it in items}:
        for t in tenants:
            b, a = before.enabled(t.id, rtype), after.enabled(t.id, rtype)
            label = f"{t.short_name or t.name} · {RESOURCE_TYPES[rtype]['name']}"
            if b and not a:
                stop.append(label)
                alarms += db.scalar(select(func.count()).select_from(AlarmEvent).where(
                    AlarmEvent.resource_type == rtype, AlarmEvent.tenant_id == t.id,
                    AlarmEvent.status.in_(ACTIVE_STATUSES))) or 0
            elif a and not b:
                start.append(label)
    return {"stop": stop, "start": start, "active_alarms": alarms}
