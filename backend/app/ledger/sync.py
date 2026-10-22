"""台账同步：拉取 → 与台账逐条比对 → 新增 / 变更 / 释放写入 resource_change → 记录一次任务运行（ledger_sync）。

设计要点：
- 管理字段（负责人、业务系统、环境、备注、核心标记）只由页面维护，同步不会覆盖
- 某类资源拉取失败时跳过该类，不做“释放”判定，避免接口故障导致整类资源被误判为已释放
- 接口返回空列表而台账中该类资源较多时，同样视为异常跳过（EMPTY_GUARD）
- 多租户：按“租户 × 项目”逐个同步，比对范围限定在本项目（project_ref）内，不会把其他租户的资源判为“已释放”
"""
import logging
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hcs import HcsClient, HcsError
from app.models import InspectionRun, Resource, ResourceChange
from app.models.base import utcnow
from app.services.catalog import RESOURCE_TYPES, type_name
from app.services import collect_switch, platform_watch
from app.services.tenancy import Target, legacy_target, resolve_targets

from .fetchers import FETCHERS, LedgerRecord

log = logging.getLogger(__name__)

LEDGER_TASK = "ledger_sync"
EMPTY_GUARD = 3
TRACKED = [("name", "名称"), ("status_raw", "状态"), ("az", "可用区"), ("ip", "IP 地址"), ("spec", "规格"),
           ("vcpus", "vCPU"), ("memory_gb", "内存(GB)"), ("storage_gb", "存储(GB)")]
MANUAL_FIELDS = {"owner": "负责人", "business": "业务系统", "env_type": "环境", "remark": "备注", "critical": "核心资源",
                 "monitored": "纳入巡检"}


def _attr_labels(rtype: str) -> dict[str, str]:
    return {c["key"][6:]: c["label"] for c in RESOURCE_TYPES[rtype]["columns"] if c["key"].startswith("attrs.")}


def _values(rec: LedgerRecord) -> dict:
    return {"name": rec.name, "status_raw": rec.status, "az": rec.az, "ip": rec.ip, "spec": rec.spec,
            "vcpus": rec.vcpus, "memory_gb": rec.memory_gb, "storage_gb": rec.storage_gb}


def _diff(r: Resource, rec: LedgerRecord) -> list[dict]:
    new = _values(rec)
    out = [{"field": f, "label": label, "old": getattr(r, f), "new": new[f]}
           for f, label in TRACKED if new[f] is not None and getattr(r, f) != new[f]]
    old_attrs = r.attrs or {}
    for key, label in _attr_labels(r.resource_type).items():
        if key in rec.attrs and rec.attrs[key] != old_attrs.get(key) and key in old_attrs:
            out.append({"field": f"attrs.{key}", "label": label, "old": old_attrs.get(key), "new": rec.attrs[key]})
    return out


def _change(db: Session, r: Resource, change_type: str, now: datetime, changes: list | None = None,
            source: str = "SYNC", operator: str = "system") -> None:
    db.add(ResourceChange(tenant_id=r.tenant_id, resource_ref=r.id, resource_type=r.resource_type,
                          resource_id=r.resource_id, name=r.name, change_type=change_type, changes=changes or [],
                          source=source, operator=operator, detected_at=now))


def apply_snapshot(db: Session, rtype: str, records: list[LedgerRecord], now: datetime | None = None,
                   region: str | None = None, tenant_id: int | None = None, project_ref: int | None = None) -> dict:
    """把某个项目某类资源的最新清单合并进台账，返回 {"total", "created", "updated", "deleted", "skipped_delete"}。"""
    now = now or utcnow()
    meta = RESOURCE_TYPES[rtype]
    q = select(Resource).where(Resource.resource_type == rtype)
    q = q.where(Resource.project_ref == project_ref) if project_ref is not None else q.where(Resource.project_ref.is_(None))
    existing = {r.resource_id: r for r in db.scalars(q)}
    # 不在本项目范围内、但资源 ID 已存在的记录（例如升级前未打租户标记的历史数据）：直接归入本项目
    unknown = [rec.resource_id for rec in records if rec.resource_id not in existing]
    if unknown:
        for old in db.scalars(select(Resource).where(Resource.resource_type == rtype, Resource.resource_id.in_(unknown))):
            existing[old.resource_id] = old
    stats = {"total": len(records), "created": 0, "updated": 0, "deleted": 0, "skipped_delete": False}
    seen: set[str] = set()
    for rec in records:
        seen.add(rec.resource_id)
        r = existing.get(rec.resource_id)
        if r is None or r.lifecycle == "DELETED":
            if r is None:
                r = Resource(service=meta["service"], resource_type=rtype, resource_id=rec.resource_id, name=rec.name,
                             labels={}, attrs={}, metrics={}, expected_state={}, first_seen=now,
                             health="UNKNOWN" if meta.get("group") else "OK")
                db.add(r)
            r.lifecycle, r.deleted_at = "ACTIVE", None
            pending: tuple[str, list[dict]] = ("CREATED", [])
            status_change: list[dict] = []
        else:
            changes = _diff(r, rec)
            status_change = [c for c in changes if c["field"] == "status_raw"]     # 状态变化单独记为“状态变更”
            changes = [c for c in changes if c["field"] != "status_raw"]
            pending = ("UPDATED", changes) if changes else ("", [])
        if rec.status and rec.status != r.status_raw:
            r.status_since = now
        for f, v in _values(rec).items():
            if v is not None:
                setattr(r, f, v)
        r.attrs = {**(r.attrs or {}), **{k: v for k, v in rec.attrs.items() if v is not None}}
        r.region = region or r.region
        r.tenant_id, r.project_ref = tenant_id if tenant_id is not None else r.tenant_id, project_ref or r.project_ref
        r.created_time = rec.created_time or r.created_time
        r.last_seen = now
        if pending[0]:
            db.flush()
            _change(db, r, pending[0], now, pending[1])
            stats["created" if pending[0] == "CREATED" else "updated"] += 1
        if status_change:
            _change(db, r, "STATUS", now, status_change)
            stats["updated"] += 0 if pending[0] else 1
    missing = [r for rid, r in existing.items() if rid not in seen and r.lifecycle == "ACTIVE"
               and r.project_ref == project_ref]
    if not records and len(missing) > EMPTY_GUARD:
        stats["skipped_delete"] = True
        log.warning("ledger %s: API returned empty list, skip %d deletions", rtype, len(missing))
    else:
        for r in missing:
            r.lifecycle, r.deleted_at = "DELETED", now
            _change(db, r, "DELETED", now)
            stats["deleted"] += 1
    db.flush()
    return stats


def record_manual_change(db: Session, r: Resource, before: dict, operator: str) -> None:
    changes = [{"field": k, "label": MANUAL_FIELDS.get(k, k), "old": v, "new": getattr(r, k)}
               for k, v in before.items() if getattr(r, k) != v and k in MANUAL_FIELDS]
    if changes:
        _change(db, r, "UPDATED", utcnow(), changes, source="MANUAL", operator=operator)


def sync_ledger(db: Session, *, client: HcsClient | None = None, target: Target | None = None,
                types: list[str] | None = None, trigger: str = "SCHEDULE") -> InspectionRun:
    """同步一个租户项目的台账（target 为空时按环境变量单账号兜底）。"""
    target = target or legacy_target()
    run = InspectionRun(task_code=LEDGER_TASK, tenant_id=target.tenant_id, trigger_type=trigger, started_at=utcnow(),
                        status="RUNNING", stats={})
    db.add(run)
    db.commit()
    try:
        client = client or target.client()
    except HcsError as e:
        run.status, run.error_summary, run.finished_at = "FAILED", str(e), utcnow()
        db.commit()
        platform_watch.on_run(db, run)
        return run
    scoped = (Resource.project_ref == target.project_ref) if target.project_ref is not None \
        else Resource.project_ref.is_(None)
    # 预置 ECS 名称、VPC 名称，单独同步 EVS / 子网时也能显示挂载关系
    ctx: dict = {"ecs_names": dict(db.execute(select(Resource.resource_id, Resource.name)
                                              .where(Resource.resource_type == "ecs_server", scoped)).all()),
                 "vpc_names": dict(db.execute(select(Resource.resource_id, Resource.name)
                                              .where(Resource.resource_type == "vpc", scoped)).all())}
    switches = collect_switch.load(db)
    wanted = [t for t in FETCHERS if (not types or t in types) and switches.enabled(target.tenant_id, t)]
    skipped = [t for t in FETCHERS if (not types or t in types) and t not in wanted]
    if not wanted:                                 # 全部类型都已关闭采集（系统管理 → 采集开关）
        run.status, run.finished_at = "SKIPPED", utcnow()
        run.error_summary = "已关闭采集：" + "、".join(type_name(t) for t in skipped)
        run.stats = {"skipped": skipped}
        db.commit()
        return run
    errors: list[str] = []
    stats: dict[str, dict] = {}
    now = utcnow()
    for rtype in wanted:
        try:
            records = FETCHERS[rtype](client, ctx)
        except (HcsError, KeyError) as e:
            errors.append(f"{type_name(rtype)}: {e}")
            continue
        stats[rtype] = apply_snapshot(db, rtype, records, now, region=target.region, tenant_id=target.tenant_id,
                                      project_ref=target.project_ref)
    db.commit()
    changed = sum(v["created"] + v["updated"] + v["deleted"] for v in stats.values())
    run.resource_total = sum(v["total"] for v in stats.values())
    run.stats = {**stats, "skipped": skipped} if skipped else stats
    run.abnormal_total = changed                       # 台账同步任务里表示“本次发现的变更数”
    run.status = "FAILED" if not stats else ("PARTIAL" if errors else "SUCCESS")
    run.error_summary = "\n".join(errors[:20]) or None
    run.finished_at, run.api_calls = utcnow(), client.calls
    db.commit()
    log.info("ledger sync [%s] %s: %d resources, %d changes", target.code, run.status, run.resource_total, changed)
    platform_watch.on_run(db, run)
    return run


def sync_all(db: Session, *, types: list[str] | None = None, trigger: str = "SCHEDULE",
             tenant_ids: list[int] | None = None, http: httpx.Client | None = None,
             mock_url: str | None = None) -> list[InspectionRun]:
    runs = []
    for t in resolve_targets(db, tenant_ids):
        try:
            client = t.client(http=http, mock_url=mock_url)
        except HcsError:
            client = None
        runs.append(sync_ledger(db, client=client, target=t, types=types, trigger=trigger))
    return runs
