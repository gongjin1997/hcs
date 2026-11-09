"""采集范围：在采集开关之下再细分对象 —— 只巡检（或屏蔽）某些命名空间、某个实例、某个工作空间下的作业 ……

- 存储：sys_setting，键 collect_scope:{租户ID}:{资源类型}，租户 0 为全局默认；租户有自己的设置时覆盖全局，没有则跟随全局
- 判定：app.collectors.scope（三值逻辑）。采集器在调用云接口之前裁剪对象，巡检流水线拿到完整信息后做最终判定
- 物化：resource.scope_excluded。巡检任务开始前、台账同步后、保存规则时重新计算；范围外的资源不采集、不评估、不告警。
  新排除的资源，其活动告警被行政关闭（留处理记录，不是指标恢复），避免留下永远不会恢复的告警
- 范围只作用于巡检与告警：台账（CMDB）仍完整记录云上资源，只在列表里标注“范围外”
- 与采集开关的关系：开关关闭 = 整类资源不采集；范围 = 开关开启后，只采集其中一部分
"""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from sqlalchemy import case, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.collectors.base import Observation
from app.collectors.scope import (
    EFFECTS,
    MAX_PATTERN_LEN,
    MAX_PATTERNS,
    MAX_RULES,
    OPEN,
    ScopeFilter,
    Value,
    compile_scope,
    ledger_subject,
    subject_of,
)
from app.core.response import BizError, ErrorCode
from app.models import AlarmEvent, AlarmEventLog, EvalState, HcsAccount, Resource, SysSetting
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow

from . import job_selection
from .catalog import RESOURCE_TYPES, SCOPE_DIM_LABELS, SCOPE_DIMENSIONS

log = logging.getLogger(__name__)
GLOBAL = 0
KEY_PREFIX = "collect_scope"
CLOSE_NOTE = "已排除出采集范围，行政关闭告警（非指标恢复）"
PAGE = 500
SCAN_LIMIT = 20000                 # 候选值与预览最多扫描的资源数
SAMPLE = 10                        # 预览里每类样例最多条数
# 只有这些类型的维度取值需要 attrs；其余类型只用资源 ID / 名称，扫描时不读 JSON 列
_ATTR_TYPES = frozenset(("dgc_realtime_job", "dgc_batch_job", "dgc_mrs_node", "mrs_job", "cce_workload"))


def setting_key(tenant_id: int, resource_type: str) -> str:
    return f"{KEY_PREFIX}:{tenant_id}:{resource_type}"


def _rules_of(value: object) -> list[dict]:
    rules = value.get("rules") if isinstance(value, Mapping) else None
    return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []


# ---------------------------------------------------------------- 读取
@dataclass
class Scopes:
    """已保存的全部采集范围（租户 × 资源类型）。一次任务只读一次库。"""

    raw: dict[tuple[int, str], dict] = field(default_factory=dict)
    meta: dict[tuple[int, str], tuple[str | None, datetime | None]] = field(default_factory=dict)
    job_selections: dict[tuple[int, int, str], dict] = field(default_factory=dict)
    _compiled: dict[tuple[int, str], ScopeFilter] = field(default_factory=dict, repr=False)

    def explicit(self, tenant_id: int, resource_type: str) -> list[dict] | None:
        """该租户（0 = 全局）自己保存的规则；没有保存 = None。"""
        value = self.raw.get((tenant_id, resource_type))
        return None if value is None else _rules_of(value)

    def _key(self, tenant_id: int | None, resource_type: str) -> tuple[int, str] | None:
        """生效的那条设置：租户自己的优先，没有则跟随全局，都没有 = None。"""
        if tenant_id is not None and (tenant_id, resource_type) in self.raw:
            return tenant_id, resource_type
        return (GLOBAL, resource_type) if (GLOBAL, resource_type) in self.raw else None

    def source(self, tenant_id: int | None, resource_type: str) -> str:
        key = self._key(tenant_id, resource_type)
        return "none" if key is None else "global" if key[0] == GLOBAL else "tenant"

    def rules(self, tenant_id: int | None, resource_type: str) -> list[dict]:
        key = self._key(tenant_id, resource_type)
        return [] if key is None else _rules_of(self.raw[key])

    def get(self, tenant_id: int | None, resource_type: str, *, project_ref: int | None = None):
        key = self._key(tenant_id, resource_type)
        if key is not None and key not in self._compiled:
            self._compiled[key] = compile_scope(self.raw[key])
        base = self._compiled[key] if key is not None else OPEN
        if resource_type in job_selection.TYPE_CODES and project_ref is not None:
            selection_key = (tenant_id, project_ref, resource_type)
            if selection_key in self.job_selections:
                return job_selection.JobSelectionScope(base, self.job_selections[selection_key])
        return base


def load(db: Session) -> Scopes:
    # 只取列值：长生命周期 worker Session 不能复用保存前的 ORM identity-map 值。
    rows = db.execute(select(SysSetting.key, SysSetting.value, SysSetting.updated_by, SysSetting.updated_at)
                      .where(SysSetting.key.startswith(f"{KEY_PREFIX}:", autoescape=True)))
    scopes = Scopes()
    for key, value, updated_by, updated_at in rows:
        parts = key.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in RESOURCE_TYPES or not isinstance(value, dict):
            continue
        scopes.raw[(int(parts[1]), parts[2])] = value
        scopes.meta[(int(parts[1]), parts[2])] = (updated_by, updated_at)
    scopes.job_selections = job_selection.load(db)
    return scopes


# ---------------------------------------------------------------- 对象 → 维度取值
def resource_subject(resource_type: str, resource_id: str, name: str | None, attrs: Mapping | None,
                     env_type: str | None, business: str | None, owner: str | None,
                     critical: bool | None) -> dict[str, Value]:
    return {**subject_of(resource_type, resource_id, name, attrs), **ledger_subject(env_type, business, owner, critical)}


def _group_subject(key: tuple[str, str], group: list[Observation], row: Resource | None) -> dict[str, Value]:
    """流水线里一个资源（同一次采集的若干观测）的维度取值：台账记录 + 本次观测，与 upsert_resource 合并属性的方式一致。"""
    attrs = dict(row.attrs or {}) if row is not None else {}
    for observation in group:
        attrs.update(observation.attrs)
    return resource_subject(key[0], key[1], group[0].name, attrs,
                            row.env_type if row else None, row.business if row else None,
                            row.owner if row else None, row.critical if row else False)


# ---------------------------------------------------------------- 标记与告警
def _close_alarms(db: Session, tenant_id: int | None, resource_ids: list[int], operator: str, now: datetime) -> int:
    """排除时一并中断连续证据，恢复范围后必须重新累计；没有活动告警的资源同样处理。"""
    from . import inspection_evidence

    closed = 0
    for start in range(0, len(resource_ids), PAGE):
        ids = resource_ids[start:start + PAGE]
        for state in db.scalars(select(EvalState).join(
                Resource, EvalState.data[inspection_evidence.META]["resource_ref"].as_integer() == Resource.id).where(
                    Resource.id.in_(ids), Resource.tenant_id.is_not_distinct_from(tenant_id),
                    Resource.scope_excluded.is_(True)).order_by(EvalState.fingerprint).with_for_update(of=EvalState)
                .execution_options(populate_existing=True)):
            state.data = inspection_evidence.interrupt(dict(state.data or {}), now)
        for alarm in db.scalars(select(AlarmEvent).where(
                AlarmEvent.resource_ref.in_(ids),
                AlarmEvent.tenant_id.is_not_distinct_from(tenant_id), AlarmEvent.status.in_(ACTIVE_STATUSES))):
            previous, alarm.status = alarm.status, "CLOSED"
            alarm.resolved_at = alarm.resolved_at or now
            db.add(AlarmEventLog(alarm_id=alarm.id, action="CLOSE", from_status=previous, to_status="CLOSED",
                                 operator=operator, note=CLOSE_NOTE, at=now))
            closed += 1
    return closed


def _set_flag(db: Session, resource_ids: list[int], excluded: bool) -> None:
    values: dict = {"scope_excluded": excluded}
    if excluded:
        values["health"] = "UNKNOWN"       # 范围外不做健康判定；列表以“范围外”标注
    for start in range(0, len(resource_ids), PAGE):
        # 默认的 synchronize_session 会同步 Session 里已加载的对象：长生命周期 Session 不会读到修改前的标记
        db.execute(update(Resource).where(Resource.id.in_(resource_ids[start:start + PAGE])).values(**values))


def filter_groups(db: Session, tenant_id: int | None, groups: dict[tuple[str, str], list[Observation]],
                  known: dict[tuple[str, str], Resource], scopes: Scopes, *, operator: str = "system",
                  project_ref: int | None = None
                  ) -> dict[tuple[str, str], list[Observation]]:
    """流水线的最终判定：返回仍在范围内的分组。

    已有记录、但落到范围外的资源就地打上标记并关闭活动告警；范围外且还没有记录的资源不创建记录
    （台账类资源由台账同步创建，随后被标记）。标记为范围外、现在回到范围内的资源撤销标记。
    """
    kept: dict[tuple[str, str], list[Observation]] = {}
    newly: list[Resource] = []
    for key, group in groups.items():
        row = known.get(key)
        target_project = project_ref if project_ref is not None else row.project_ref if row is not None else None
        flt = scopes.get(tenant_id, key[0], project_ref=target_project)
        # 未携带项目上下文时不可把某项目的名单借给另一项目的新对象。
        if target_project is None and any(tid == tenant_id and kind == key[0]
                for tid, _project, kind in scopes.job_selections):
            flt = job_selection.JobSelectionScope(flt, {})
        if flt.open or flt.check(_group_subject(key, group, row)) is not False:
            kept[key] = group
            if row is not None and row.scope_excluded:
                row.scope_excluded = False
        elif row is not None and not row.scope_excluded:
            row.scope_excluded, row.health = True, "UNKNOWN"
            newly.append(row)
    if newly:
        db.flush()
        _close_alarms(db, tenant_id, [r.id for r in newly], operator, utcnow())
    return kept


def _resource_conditions(tenant_id: int | None, resource_type: str, project_ref: int | None) -> list:
    conditions = [Resource.resource_type == resource_type,
                  Resource.tenant_id.is_(None) if tenant_id is None else Resource.tenant_id == tenant_id]
    if project_ref is not None:
        conditions.append(Resource.project_ref == project_ref)
    return conditions


def _scan(db: Session, resource_type: str, *, tenant_ids: list[int | None] | None,
          project_ref: int | None = None, limit: int | None = None) -> Iterator:
    """按主键分页读取活动资源的维度来源列；每页一次查询，不在页间持有游标。"""
    columns: list[InstrumentedAttribute] = [
        Resource.id, Resource.tenant_id, Resource.project_ref, Resource.resource_id, Resource.name, Resource.env_type,
        Resource.business, Resource.owner, Resource.critical, Resource.scope_excluded,
    ]
    if resource_type in _ATTR_TYPES:
        columns.append(Resource.attrs)
    query = select(*columns).where(Resource.resource_type == resource_type, Resource.lifecycle == "ACTIVE")
    if project_ref is not None:
        query = query.where(Resource.project_ref == project_ref)
    if tenant_ids is not None:
        real = [t for t in tenant_ids if t is not None]
        query = query.where(Resource.tenant_id.in_(real) | Resource.tenant_id.is_(None) if None in tenant_ids
                            else Resource.tenant_id.in_(real))
    last, seen = 0, 0
    while True:
        rows = db.execute(query.where(Resource.id > last).order_by(Resource.id).limit(PAGE)).all()
        if not rows:
            return
        yield from rows
        last, seen = rows[-1].id, seen + len(rows)
        if limit is not None and seen >= limit:
            return


def _row_subject(resource_type: str, row) -> dict[str, Value]:
    return resource_subject(resource_type, row.resource_id, row.name, getattr(row, "attrs", None),
                            row.env_type, row.business, row.owner, row.critical)


def sync_flags(db: Session, tenant_id: int | None, resource_types: list[str], *, project_ref: int | None = None,
               operator: str = "system", scopes: Scopes | None = None) -> dict:
    """按当前规则重新计算某租户（可限定项目）这些资源类型的“范围外”标记，并关闭新排除资源的活动告警。

    范围为“全部”时只做一次批量更新，撤销残留标记；有规则时分页读取维度来源列比较，只写变化的行。
    """
    scopes = scopes or load(db)
    now = utcnow()
    out = {"excluded": 0, "restored": 0, "closed_alarms": 0}
    for rtype in resource_types:
        if rtype not in RESOURCE_TYPES:
            continue
        flt = scopes.get(tenant_id, rtype)
        has_selection = any(tid == tenant_id and kind == rtype and (project_ref is None or project == project_ref)
                            for tid, project, kind in scopes.job_selections)
        if flt.open and not has_selection:
            result = db.execute(update(Resource).where(*_resource_conditions(tenant_id, rtype, project_ref),
                                                       Resource.scope_excluded.is_(True))
                                .values(scope_excluded=False))
            out["restored"] += cast(CursorResult, result).rowcount or 0
            continue
        exclude: list[int] = []
        restore: list[int] = []
        for row in _scan(db, rtype, tenant_ids=[tenant_id], project_ref=project_ref):
            row_filter = scopes.get(tenant_id, rtype, project_ref=row.project_ref)
            excluded = row_filter.check(_row_subject(rtype, row)) is False
            if excluded and not row.scope_excluded:
                exclude.append(row.id)
            elif not excluded and row.scope_excluded:
                restore.append(row.id)
        _set_flag(db, exclude, True)
        _set_flag(db, restore, False)
        out["excluded"] += len(exclude)
        out["restored"] += len(restore)
        out["closed_alarms"] += _close_alarms(db, tenant_id, exclude, operator, now)
    db.flush()
    return out


def refresh_resource(db: Session, resource: Resource, *, operator: str = "system",
                     scopes: Scopes | None = None) -> bool:
    """单个资源的台账字段被修改后（负责人 / 业务系统 / 环境 / 核心资源）重新判定；返回是否变化。
    批量修改（Excel 导入）时由调用方传入同一份 scopes，避免逐行读库。"""
    if resource.resource_type not in RESOURCE_TYPES or resource.lifecycle != "ACTIVE":
        return False
    flt = (scopes or load(db)).get(resource.tenant_id, resource.resource_type, project_ref=resource.project_ref)
    excluded = not flt.open and flt.check(resource_subject(
        resource.resource_type, resource.resource_id, resource.name, resource.attrs, resource.env_type,
        resource.business, resource.owner, resource.critical)) is False
    if excluded == resource.scope_excluded:
        return False
    resource.scope_excluded = excluded
    if excluded:
        resource.health = "UNKNOWN"
        db.flush()
        _close_alarms(db, resource.tenant_id, [resource.id], operator, utcnow())
    return True


# ---------------------------------------------------------------- 规则校验与保存
def describe_rules(rules: list[dict]) -> list[str]:
    """规则的可读文本，用于审计与提示：排除：命名空间 = test-*、dev ｜ 仅包含：工作空间 = ws1"""
    lines = []
    for rule in rules:
        conditions = "；".join(f"{SCOPE_DIM_LABELS.get(dim, dim)} = {'、'.join(patterns)}"
                               for dim, patterns in (rule.get("match") or {}).items())
        lines.append(f"{'仅包含' if rule.get('effect') == 'include' else '排除'}：{conditions}")
    return lines


def normalize_rules(resource_type: str, rules: Sequence[Mapping]) -> list[dict]:
    """校验并规整页面提交的规则：维度必须是该资源类型提供的，模式去空白去重，数量与长度有上限。"""
    meta = RESOURCE_TYPES.get(resource_type)
    if meta is None:
        raise BizError(ErrorCode.BAD_REQUEST, f"未知资源类型：{resource_type}")
    allowed = SCOPE_DIMENSIONS[resource_type]
    if len(rules) > MAX_RULES:
        raise BizError(ErrorCode.BAD_REQUEST, f"一个资源类型最多 {MAX_RULES} 条范围规则")
    out: list[dict] = []
    for index, rule in enumerate(rules, start=1):
        if rule.get("effect") not in EFFECTS:
            raise BizError(ErrorCode.BAD_REQUEST, f"第 {index} 条规则的类型须为“仅包含”或“排除”")
        cleaned: dict[str, list[str]] = {}
        for dim, patterns in (rule.get("match") or {}).items():
            if dim not in allowed:
                names = "、".join(SCOPE_DIM_LABELS.get(d, d) for d in allowed)
                label = SCOPE_DIM_LABELS.get(dim, dim)
                raise BizError(ErrorCode.BAD_REQUEST, f"第 {index} 条规则：{meta['name']}不支持按“{label}”匹配，可用：{names}")
            values = list(dict.fromkeys(p.strip() for p in patterns if isinstance(p, str) and p.strip()))
            if not values:
                continue
            if len(values) > MAX_PATTERNS or any(len(v) > MAX_PATTERN_LEN for v in values):
                raise BizError(ErrorCode.BAD_REQUEST,
                               f"第 {index} 条规则：每个条件最多 {MAX_PATTERNS} 个值，单个值不超过 {MAX_PATTERN_LEN} 个字符")
            if dim == "critical" and any(v not in ("true", "false") for v in values):
                raise BizError(ErrorCode.BAD_REQUEST, f"第 {index} 条规则：核心资源只能填 true 或 false")
            cleaned[dim] = values
        if not cleaned:
            raise BizError(ErrorCode.BAD_REQUEST, f"第 {index} 条规则没有填写任何匹配条件")
        item: dict = {"effect": rule["effect"], "match": {dim: cleaned[dim] for dim in allowed if dim in cleaned}}
        note = str(rule.get("note") or "").strip()
        if note:
            item["note"] = note[:200]
        out.append(item)
    return out


def affected_tenants(db: Session, tenant_id: int, resource_type: str) -> list[int | None]:
    """保存 tenant_id 的设置后，哪些租户的有效范围可能变化：租户自己；全局则是所有跟随全局的租户。"""
    if tenant_id != GLOBAL:
        return [tenant_id]
    scopes = load(db)
    owners = set(db.scalars(select(Resource.tenant_id).where(Resource.resource_type == resource_type).distinct()))
    return [t for t in sorted(owners, key=lambda x: (x is None, x or 0))
            if t is None or scopes.source(t, resource_type) != "tenant"]


def save(db: Session, tenant_id: int, resource_type: str, rules: Sequence[Mapping] | None, operator: str) -> dict:
    """保存规则并立即同步标记。rules=None：租户改为跟随全局（删除单独设置）；全局设置为空 = 全部采集。
    调用方负责授权与提交（与审计同一事务）。"""
    if resource_type not in RESOURCE_TYPES:
        raise BizError(ErrorCode.BAD_REQUEST, f"未知资源类型：{resource_type}")
    if tenant_id != GLOBAL and db.get(HcsAccount, tenant_id) is None:
        raise BizError(ErrorCode.NOT_FOUND, "租户不存在", 404)
    if tenant_id == GLOBAL and rules is None:
        raise BizError(ErrorCode.BAD_REQUEST, "全局范围不能“跟随全局”，清空规则即为全部采集")
    key = setting_key(tenant_id, resource_type)
    before = load(db).explicit(tenant_id, resource_type)
    after = None if rules is None else normalize_rules(resource_type, rules)
    row = db.get(SysSetting, key, populate_existing=True)
    if after is None or (tenant_id == GLOBAL and not after):
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(SysSetting(key=key, value={"rules": after}, updated_by=operator))
    else:
        row.value, row.updated_by = {"rules": after}, operator
    db.flush()
    scopes = load(db)
    totals = {"excluded": 0, "restored": 0, "closed_alarms": 0}
    for tid in affected_tenants(db, tenant_id, resource_type):
        result = sync_flags(db, tid, [resource_type], operator=operator, scopes=scopes)
        for name in totals:
            totals[name] += result[name]
    # 全局：没有设置与空规则都是“全部采集”；租户：没有设置（跟随全局）与空规则（明确不限制）含义不同
    changed = (before or []) != (after or []) if tenant_id == GLOBAL else before != after
    return {"changed": changed, "before": before, "after": after, **totals}


# ---------------------------------------------------------------- 预览与候选值
def preview(db: Session, tenant_id: int, resource_type: str, rules: Sequence[Mapping] | None,
            visible_tenants: list[int] | None = None) -> dict:
    """保存前预估：这组规则会让多少资源落到范围外、哪些是新排除的、将关闭多少活动告警。
    rules=None 表示租户改为跟随全局，按全局规则估算。"""
    if resource_type not in RESOURCE_TYPES:
        raise BizError(ErrorCode.BAD_REQUEST, f"未知资源类型：{resource_type}")
    if rules is None:
        effective = load(db).explicit(GLOBAL, resource_type) or []
    else:
        effective = normalize_rules(resource_type, rules)
    flt = compile_scope({"rules": effective})
    selections = job_selection.load(db) if resource_type in job_selection.TYPE_CODES else {}
    tenants = affected_tenants(db, tenant_id, resource_type)
    if visible_tenants is not None:                       # 只估算用户数据范围内的租户
        tenants = [t for t in tenants if t is not None and t in visible_tenants]
    total = included = excluded = 0
    newly_excluded: list[int] = []
    newly_included = 0
    samples: dict[str, list[str]] = {"excluded": [], "included": []}
    for tid in tenants:
        for row in _scan(db, resource_type, tenant_ids=[tid], limit=SCAN_LIMIT):
            total += 1
            selected: dict | None = None
            project_ref = row.project_ref
            if tid is not None and project_ref is not None:
                selected = selections.get((tid, project_ref, resource_type))
            row_filter = job_selection.JobSelectionScope(flt, selected) if selected is not None else flt
            is_excluded = not row_filter.open and row_filter.check(_row_subject(resource_type, row)) is False
            if is_excluded:
                excluded += 1
                if not row.scope_excluded:
                    newly_excluded.append(row.id)
                if len(samples["excluded"]) < SAMPLE:
                    samples["excluded"].append(row.name or row.resource_id)
            else:
                included += 1
                newly_included += bool(row.scope_excluded)
                if len(samples["included"]) < SAMPLE:
                    samples["included"].append(row.name or row.resource_id)
    alarms = 0
    for start in range(0, len(newly_excluded), PAGE):
        alarms += db.scalar(select(func.count()).select_from(AlarmEvent).where(
            AlarmEvent.resource_ref.in_(newly_excluded[start:start + PAGE]),
            AlarmEvent.status.in_(ACTIVE_STATUSES))) or 0
    return {"total": total, "included": included, "excluded": excluded, "newly_excluded": len(newly_excluded),
            "newly_included": newly_included, "closing_alarms": alarms, "samples": samples,
            "truncated": total >= SCAN_LIMIT, "rules": effective}


def candidates(db: Session, tenant_id: int, resource_type: str, dim: str, keyword: str | None = None,
               limit: int = 50, visible_tenants: list[int] | None = None) -> list[dict]:
    """页面选择条件时的候选值：该租户（全局 = 所有租户）已知资源在这个维度上的取值及数量。
    只覆盖已经采集或同步过的对象；没有出现过的值（例如从未采集的命名空间）可以手工输入。"""
    if resource_type not in RESOURCE_TYPES:
        raise BizError(ErrorCode.BAD_REQUEST, f"未知资源类型：{resource_type}")
    if dim not in SCOPE_DIMENSIONS[resource_type]:
        raise BizError(ErrorCode.BAD_REQUEST, f"{RESOURCE_TYPES[resource_type]['name']}不支持按“{dim}”匹配")
    if tenant_id == GLOBAL:
        tenants: list[int | None] | None = None if visible_tenants is None else list(visible_tenants)
    else:
        tenants = [tenant_id]
    counts: Counter[str] = Counter()
    needle = (keyword or "").strip().lower()
    for row in _scan(db, resource_type, tenant_ids=tenants, limit=SCAN_LIMIT):
        value = _row_subject(resource_type, row).get(dim)
        if isinstance(value, tuple):                         # 集群：优先显示名称，没有名称时显示 ID
            value = next((v for v in reversed(value) if v), "")
        if value and (not needle or needle in value.lower()):
            counts[value] += 1
    return [{"value": value, "count": count}
            for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


# ---------------------------------------------------------------- 概览（采集开关页）
def overview(db: Session, tenant_ids: list[int] | None) -> dict:
    """每个资源类型的全局范围、各租户的设置与生效规则、资源数与范围外资源数。"""
    scopes = load(db)
    query = select(HcsAccount).order_by(HcsAccount.id)
    if tenant_ids is not None:
        query = query.where(HcsAccount.id.in_(tenant_ids))
    tenants = list(db.scalars(query))
    tids = [t.id for t in tenants]
    stats: dict[tuple[int, str], tuple[int, int]] = {
        (tid, rtype): (int(total), int(out or 0)) for tid, rtype, total, out in db.execute(
            select(Resource.tenant_id, Resource.resource_type, func.count(),
                   func.sum(case((Resource.scope_excluded.is_(True), 1), else_=0)))
            .where(Resource.lifecycle == "ACTIVE", Resource.tenant_id.in_(tids))
            .group_by(Resource.tenant_id, Resource.resource_type))}
    types = []
    for code, meta in RESOURCE_TYPES.items():
        by_tenant = {}
        for t in tenants:
            total, out = stats.get((t.id, code), (0, 0))
            updated_by, updated_at = scopes.meta.get((t.id, code), (None, None))
            by_tenant[str(t.id)] = {
                "setting": scopes.explicit(t.id, code), "source": scopes.source(t.id, code),
                "rules": scopes.rules(t.id, code), "resources": total, "excluded": out,
                "updated_by": updated_by, "updated_at": updated_at}
        updated_by, updated_at = scopes.meta.get((GLOBAL, code), (None, None))
        types.append({
            "code": code, "name": meta["name"], "service": meta["service"], "category": meta["category"],
            "dimensions": [{"key": d, "label": SCOPE_DIM_LABELS.get(d, d)} for d in SCOPE_DIMENSIONS[code]],
            "global": {"rules": scopes.explicit(GLOBAL, code) or [], "updated_by": updated_by,
                       "updated_at": updated_at},
            "tenants": by_tenant})
    return {"types": types, "limits": {"rules": MAX_RULES, "patterns": MAX_PATTERNS, "pattern_length": MAX_PATTERN_LEN},
            "tenants": [{"id": t.id, "code": t.code, "name": t.name, "short_name": t.short_name or t.name,
                         "color": t.color, "enabled": t.enabled} for t in tenants]}
