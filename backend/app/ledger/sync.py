"""台账同步：拉取 → 与台账逐条比对 → 新增 / 变更 / 释放写入 resource_change → 记录一次任务运行（ledger_sync）。

设计要点：
- 管理字段（负责人、业务系统、环境、备注、核心标记）只由页面维护，同步不会覆盖
- 某类资源拉取失败时跳过该类，不做“释放”判定，避免接口故障导致整类资源被误判为已释放
- 接口返回空列表而台账中该类资源较多时，同样视为异常跳过（EMPTY_GUARD）
- 多租户：按“租户 × 项目”逐个同步，比对范围限定在本项目（project_ref）内，不会把其他租户的资源判为“已释放”
"""
import logging
import re
from copy import deepcopy
from datetime import datetime
from time import perf_counter

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session

from app.core.logging import bind_log_context
from app.hcs import HcsClient, HcsError
from app.hcs.client import metrics_delta
from app.models import AlarmEvent, InspectionRun, Resource, ResourceChange
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow
from app.services import collect_switch, collection_policy, platform_watch, settings_service
from app.services.catalog import RESOURCE_TYPES, type_name
from app.services.tenancy import Target, legacy_target, resolve_targets

from .fetchers import FETCHERS, LedgerRecord, remember_owners

log = logging.getLogger(__name__)


class _InventoryPolicyChanged(Exception):
    pass

LEDGER_TASK = "ledger_sync"
EMPTY_GUARD = 3
_SQLSTATE = re.compile(r"[A-Z0-9]{5}\Z")
_DB_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_HCS_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
TRACKED = [("name", "名称"), ("status_raw", "状态"), ("az", "可用区"), ("ip", "IP 地址"), ("spec", "规格"),
           ("vcpus", "vCPU"), ("memory_gb", "内存(GB)"), ("storage_gb", "存储(GB)")]
MANUAL_FIELDS = {"owner": "负责人", "owner_user_id": "负责人用户 ID", "business": "业务系统", "env_type": "环境",
                 "remark": "备注", "critical": "核心资源",
                 "monitored": "纳入巡检"}


def _attr_labels(rtype: str) -> dict[str, str]:
    return {c["key"][6:]: c["label"] for c in RESOURCE_TYPES[rtype]["columns"] if c["key"].startswith("attrs.")}


def _values(rec: LedgerRecord) -> dict:
    return {"name": rec.name, "status_raw": rec.status, "az": rec.az, "ip": rec.ip, "spec": rec.spec,
            "vcpus": rec.vcpus, "memory_gb": rec.memory_gb, "storage_gb": rec.storage_gb}


def _database_error_fields(exc: DataError) -> dict[str, str | int]:
    original = getattr(exc, "orig", None)
    diagnostic = getattr(original, "diag", None)
    fields: dict[str, str | int] = {}
    state = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if isinstance(state, str) and _SQLSTATE.fullmatch(state):
        fields["sqlstate"] = state
    for name in ("table_name", "column_name"):
        value = getattr(diagnostic, name, None)
        if isinstance(value, str) and _DB_IDENTIFIER.fullmatch(value):
            fields[name] = value
    primary = getattr(diagnostic, "message_primary", None)
    if isinstance(primary, str):
        width = re.fullmatch(r"value too long for type character varying\(([0-9]{1,6})\)", primary)
        if width:
            fields["db_column_limit"] = int(width.group(1))
    return fields


def _safe_failure_fields(exc: Exception) -> dict[str, object]:
    if isinstance(exc, DataError):
        db_fields: dict[str, object] = {}
        db_fields.update(_database_error_fields(exc))
        return db_fields
    if not isinstance(exc, HcsError):
        return {}
    fields: dict[str, object] = {"upstream_status": exc.status, "retryable": exc.retryable}
    if isinstance(exc.code, str) and _HCS_CODE.fullmatch(exc.code):
        fields["upstream_code"] = exc.code
    if isinstance(exc.reason, str) and re.fullmatch(r"[a-z_]{1,32}", exc.reason):
        fields["network_reason"] = exc.reason
    if isinstance(exc.request_id, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", exc.request_id):
        fields["upstream_request_id"] = exc.request_id
    return fields


def _snapshot_failure_detail(exc: Exception) -> str:
    if isinstance(exc, HcsError):
        return str(exc)
    if isinstance(exc, DataError):
        fields = _database_error_fields(exc)
        details = [f"{key}={value}" for key, value in fields.items()]
        suffix = f"，{'，'.join(details)}" if details else ""
        return f"清单处理失败（数据库数据错误{suffix}）"
    return f"清单处理失败（{type(exc).__name__}）"


def _record_text_lengths(records: list[LedgerRecord]) -> dict[str, int]:
    fields = {"name": "name", "id": "resource_id", "status": "status", "az": "az", "ip": "ip",
              "spec": "spec"}
    return {f"max_{label}_chars": max((len(value) for record in records
                                        if isinstance(value := getattr(record, attribute), str)), default=0)
            for label, attribute in fields.items()}


def _diff(r: Resource, rec: LedgerRecord) -> list[dict]:
    new = _values(rec)
    out = [{"field": f, "label": label, "old": getattr(r, f), "new": new[f]}
           for f, label in TRACKED if (new[f] is not None or f == "status_raw" or f in rec.clear_fields)
           and getattr(r, f) != new[f]]
    old_attrs = r.attrs or {}
    attr_labels = _attr_labels(r.resource_type)
    for key in rec.clear_attrs:
        attr_labels.setdefault(key, key)
    for key, label in attr_labels.items():
        if key in rec.attrs and (rec.attrs[key] is not None or key in rec.clear_attrs) \
                and rec.attrs[key] != old_attrs.get(key) and key in old_attrs:
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
    ids = [rec.resource_id for rec in records]
    if any(not isinstance(rid, str) or not rid for rid in ids) or len(set(ids)) != len(ids):
        raise HcsError(0, "LOCAL.INVALID_RESPONSE", "资源清单包含无效或重复的资源 ID，未应用快照")
    for rec in records:
        if set(rec.clear_fields) - set(_values(rec)) or set(rec.clear_attrs) - set(rec.attrs):
            raise HcsError(0, "LOCAL.INVALID_RESPONSE", "资源清单包含无效的字段清理声明，未应用快照")
    meta = RESOURCE_TYPES[rtype]
    db.flush()
    q = select(Resource).where(Resource.resource_type == rtype, Resource.tenant_id == tenant_id)
    q = q.where(Resource.project_ref == project_ref) if project_ref is not None else q.where(Resource.project_ref.is_(None))
    existing = {r.resource_id: r for r in db.scalars(q.order_by(Resource.resource_id)
                .with_for_update().execution_options(populate_existing=True))}
    # 只收编无项目且属于当前租户/未分配租户的历史记录，其他项目的同 ID 记录绝不改动。
    unknown = [rec.resource_id for rec in records if rec.resource_id not in existing]
    if unknown and project_ref is not None:
        for old in db.scalars(select(Resource).where(
                Resource.resource_type == rtype, Resource.resource_id.in_(unknown), Resource.project_ref.is_(None),
                or_(Resource.tenant_id.is_(None), Resource.tenant_id == tenant_id))):
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
                             health="UNKNOWN")
                db.add(r)
            r.lifecycle, r.deleted_at = "ACTIVE", None
            pending: tuple[str, list[dict]] = ("CREATED", [])
            status_change: list[dict] = []
        else:
            changes = _diff(r, rec)
            status_change = [c for c in changes if c["field"] == "status_raw"]     # 状态变化单独记为“状态变更”
            changes = [c for c in changes if c["field"] != "status_raw"]
            pending = ("UPDATED", changes) if changes else ("", [])
        if rec.status != r.status_raw:
            r.status_since = now
        for f, v in _values(rec).items():
            if v is not None or f == "status_raw" or f in rec.clear_fields:
                setattr(r, f, v)
        r.attrs = {**(r.attrs or {}), **{k: v for k, v in rec.attrs.items() if v is not None or k in rec.clear_attrs}}
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


def propagate_owner(db: Session, r: Resource) -> None:
    """按资源引用更新活动告警的负责人；恢复 / 关闭的历史快照保持不变。"""
    db.execute(update(AlarmEvent).where(AlarmEvent.resource_ref == r.id,
                                       AlarmEvent.status.in_(ACTIVE_STATUSES)).values(owner=r.owner))


def record_manual_change(db: Session, r: Resource, before: dict, operator: str) -> None:
    changes = [{"field": k, "label": MANUAL_FIELDS.get(k, k), "old": v, "new": getattr(r, k)}
               for k, v in before.items() if getattr(r, k) != v and k in MANUAL_FIELDS]
    if changes:
        _change(db, r, "UPDATED", utcnow(), changes, source="MANUAL", operator=operator)
        if any(c["field"] in ("owner", "owner_user_id") for c in changes):
            propagate_owner(db, r)


def sync_ledger(db: Session, *, client: HcsClient | None = None, target: Target | None = None,
                types: list[str] | None = None, trigger: str = "SCHEDULE") -> InspectionRun:
    """同步一个租户项目的台账（target 为空时按环境变量单账号兜底）。"""
    sync_started = perf_counter()
    target = target or legacy_target()
    run = InspectionRun(task_code=LEDGER_TASK, tenant_id=target.tenant_id, project_ref=target.project_ref,
                        trigger_type=trigger, started_at=utcnow(),
                        status="RUNNING", stats={})
    db.add(run)
    db.commit()
    bind_log_context(run_id=run.id, task=LEDGER_TASK, tenant_id=target.tenant_id,
                     project_id=getattr(target, "project_id", None),
                     project_ref=target.project_ref, trigger=trigger, cloud_service="ledger")
    switches = collect_switch.load(db)
    wanted = [t for t in FETCHERS if (not types or t in types) and switches.enabled(target.tenant_id, t)]
    inventory_claim = None
    if "ecs_server" in wanted:
        inventory_claim = collection_policy.claim(db, target.tenant_id, target.project_ref, "inventory",
                                                   force=trigger != "SCHEDULE")
        if inventory_claim is None:
            wanted.remove("ecs_server")
    skipped = [t for t in FETCHERS if (not types or t in types) and t not in wanted]
    log.info("ledger_sync_started", extra={"event": "ledger_sync_started", "run_id": run.id,
             "tenant_id": target.tenant_id, "project_ref": target.project_ref, "trigger": trigger,
             "requested_count": len(types) if types is not None else len(FETCHERS),
             "enabled_count": len(wanted), "skipped_count": len(skipped)})
    if not wanted:
        run.status, run.finished_at = "SKIPPED", utcnow()
        run.error_summary = "采集已关闭、尚未到期或正在执行"
        run.stats = {"skipped": skipped}
        db.commit()
        log.info("ledger_sync_finished", extra={"event": "ledger_sync_finished", "run_id": run.id,
                 "tenant_id": target.tenant_id, "project_ref": target.project_ref, "status": run.status,
                 "resource_count": 0, "change_count": 0, "api_calls": 0, "error_count": 0,
                 "duration_ms": round((perf_counter() - sync_started) * 1000, 2)})
        return run
    try:
        client = client or target.client()
        for key in ("project_id", "tenant_id", "project_ref"):
            expected, actual = getattr(target, key), getattr(client, key, None)
            if expected not in (None, "") and actual not in (None, "") and expected != actual:
                raise HcsError(0, "LOCAL.PROJECT_MISMATCH", "客户端与台账目标项目归属不一致，未请求清单")
        calls_before, stats_before = client.calls, client.metrics_snapshot()
        client.configure_qps(settings_service.get_collect(db).hcs_qps)
    except Exception as e:
        if inventory_claim is not None:
            collection_policy.finish(db, inventory_claim, False)
        detail = str(e) if isinstance(e, HcsError) else f"客户端初始化失败（{type(e).__name__}）"
        run.status, run.error_summary, run.finished_at = "FAILED", detail, utcnow()
        db.commit()
        log.error("ledger_sync_client_failed", extra={"event": "ledger_sync_client_failed", "run_id": run.id,
                  "tenant_id": target.tenant_id, "project_ref": target.project_ref,
                  "exception_type": type(e).__name__, **_safe_failure_fields(e)})
        log.info("ledger_sync_finished", extra={"event": "ledger_sync_finished", "run_id": run.id,
                 "tenant_id": target.tenant_id, "project_ref": target.project_ref, "status": run.status,
                 "resource_count": 0, "change_count": 0, "api_calls": 0, "error_count": 1,
                 "duration_ms": round((perf_counter() - sync_started) * 1000, 2)})
        platform_watch.on_run(db, run)
        return run
    scoped = (Resource.project_ref == target.project_ref) if target.project_ref is not None \
        else Resource.project_ref.is_(None)
    # 预置 ECS 名称、VPC 名称，单独同步 EVS / 子网时也能显示挂载关系
    sources = db.execute(select(Resource.resource_type, Resource.resource_id, Resource.name, Resource.ip, Resource.attrs).where(
        Resource.resource_type.in_(("ecs_server", "elb")), scoped, Resource.tenant_id == target.tenant_id,
        Resource.lifecycle == "ACTIVE")).all()
    ctx: dict = {"ecs_names": {rid: name for rtype, rid, name, _ip, _attrs in sources if rtype == "ecs_server"},
                 "vpc_names": dict(db.execute(select(Resource.resource_id, Resource.name)
                                              .where(Resource.resource_type == "vpc", scoped,
                                                     Resource.tenant_id == target.tenant_id,
                                                     Resource.lifecycle == "ACTIVE")).all())}
    for rtype, kind, label in (("ecs_server", "ecs", "云服务器"), ("elb", "elb", "负载均衡")):
        bindings = []
        for source_type, rid, name, ip, attrs in sources:
            if source_type != rtype:
                continue
            attrs = attrs or {}
            ports = attrs.get("fixed_ports") if kind == "ecs" else (
                {attrs["vip_port_id"]: ip} if attrs.get("vip_port_id") else {})
            ports = ports if isinstance(ports, dict) else {}
            ips = attrs.get("fixed_ips") if kind == "ecs" else []
            ips = ips if isinstance(ips, list) else []
            bindings.append((rid, f"{label} {name}", [*ips, *([ip] if ip else [])], ports))
        remember_owners(ctx, kind, bindings)
    errors: list[str] = []
    stats: dict[str, dict] = {}
    now = utcnow()
    for rtype in wanted:
        snapshot_started = perf_counter()
        calls_at_snapshot_start = client.calls
        progress: dict[str, object] = {"phase": "fetch", "record_count": None}
        log.debug("ledger_snapshot_started", extra={"event": "ledger_snapshot_started", "run_id": run.id,
                  "tenant_id": target.tenant_id, "project_ref": target.project_ref,
                  "resource_type": rtype, "phase": "fetch"})
        try:
            snapshot_ctx = deepcopy(ctx)
            def collect_snapshot(rtype: str = rtype, snapshot_ctx: dict = snapshot_ctx,
                                 progress: dict = progress) -> dict:
                with db.begin_nested():
                    # Never filter inventory by monitoring selection: this is a full snapshot.
                    records = FETCHERS[rtype](client, snapshot_ctx)
                    progress["record_count"] = len(records)
                    progress["text_lengths"] = _record_text_lengths(records)
                    if rtype == "ecs_server" and not collection_policy.stage_enabled(db, target.tenant_id, "inventory"):
                        raise _InventoryPolicyChanged()
                    progress["phase"] = "database_apply"
                    return apply_snapshot(db, rtype, records, now, region=target.region, tenant_id=target.tenant_id,
                                          project_ref=target.project_ref)

            if rtype == "ecs_server" and inventory_claim is not None:
                with collection_policy.maintain_claim(db, inventory_claim):
                    result = collect_snapshot()
                db.commit()
                collection_policy.finish(db, inventory_claim, not result["skipped_delete"])
            else:
                result = collect_snapshot()
            ctx = snapshot_ctx
            stats[rtype] = result
            if rtype == "cce_cluster" and ctx.get("cce_node_failures"):
                result["node_enrichment_failures"] = ctx["cce_node_failures"]
                errors.append(f"CCE：{ctx['cce_node_failures']} 个集群节点查询失败，集群清单已更新，节点数量标记未知")
                log.warning("cce_node_enrichment_failed", extra={"event": "cce_node_enrichment_failed",
                    "tenant_id": target.tenant_id, "project_ref": target.project_ref,
                    "affected_count": ctx["cce_node_failures"], "phase": "node_enrichment"})
            log.info("ledger_snapshot_completed", extra={"event": "ledger_snapshot_completed", "run_id": run.id,
                     "tenant_id": target.tenant_id, "project_ref": target.project_ref, "resource_type": rtype,
                     "phase": "database_apply", "record_count": result["total"],
                     "fetch_calls": max(0, client.calls - calls_at_snapshot_start),
                     "created_count": result["created"], "updated_count": result["updated"],
                     "deleted_count": result["deleted"], "skipped_delete": result["skipped_delete"],
                     "duration_ms": round((perf_counter() - snapshot_started) * 1000, 2)})
            if result["skipped_delete"]:
                errors.append(f"{type_name(rtype)}: 上游空清单触发保护，已保留原有资源")
        except _InventoryPolicyChanged:
            if inventory_claim is not None:
                collection_policy.finish(db, inventory_claim, False)
            skipped.append(rtype)
            log.warning("ledger_snapshot_skipped", extra={"event": "ledger_snapshot_skipped", "run_id": run.id,
                        "tenant_id": target.tenant_id, "project_ref": target.project_ref,
                        "resource_type": rtype, "phase": str(progress["phase"]),
                        "reason": "collection_policy_changed", "record_count": progress["record_count"],
                        "fetch_calls": max(0, client.calls - calls_at_snapshot_start),
                        "duration_ms": round((perf_counter() - snapshot_started) * 1000, 2)})
        except Exception as e:
            if rtype == "ecs_server" and inventory_claim is not None:
                collection_policy.finish(db, inventory_claim, False)
            detail = _snapshot_failure_detail(e)
            failure_fields: dict[str, object] = {"event": "ledger_snapshot_failed", "run_id": run.id,
                "tenant_id": target.tenant_id, "project_ref": target.project_ref, "resource_type": rtype,
                "phase": str(progress["phase"]), "exception_type": type(e).__name__,
                "record_count": progress["record_count"],
                "fetch_calls": max(0, client.calls - calls_at_snapshot_start),
                "duration_ms": round((perf_counter() - snapshot_started) * 1000, 2)}
            text_lengths = progress.get("text_lengths")
            if isinstance(text_lengths, dict):
                failure_fields.update(text_lengths)
            log.log(logging.ERROR if isinstance(e, DataError) else logging.WARNING,
                    "ledger_snapshot_failed", extra={**failure_fields, **_safe_failure_fields(e)},
                    exc_info=(RuntimeError, RuntimeError(type(e).__name__), e.__traceback__))
            errors.append(f"{type_name(rtype)}: {detail}")
            continue
    db.commit()
    changed = sum(v["created"] + v["updated"] + v["deleted"] for v in stats.values())
    run.resource_total = sum(v["total"] for v in stats.values())
    run.stats = {**stats, "skipped": skipped} if skipped else stats
    run.stats = {**run.stats, "hcs": metrics_delta(client.metrics_snapshot(), stats_before)}
    run.abnormal_total = changed                       # 台账同步任务里表示“本次发现的变更数”
    run.status = ("FAILED" if errors else "SKIPPED") if not stats else ("PARTIAL" if errors else "SUCCESS")
    run.error_summary = "\n".join(errors[:20]) or None
    run.finished_at, run.api_calls = utcnow(), max(0, client.calls - calls_before)
    db.commit()
    log.info("ledger_sync_finished", extra={"event": "ledger_sync_finished", "run_id": run.id,
             "tenant_id": target.tenant_id, "project_ref": target.project_ref, "status": run.status,
             "resource_count": run.resource_total, "change_count": changed, "api_calls": run.api_calls,
             "error_count": len(errors), "enabled_count": len(stats), "skipped_count": len(skipped),
             "duration_ms": round((perf_counter() - sync_started) * 1000, 2)})
    platform_watch.on_run(db, run)
    return run


def sync_all(db: Session, *, types: list[str] | None = None, trigger: str = "SCHEDULE",
             tenant_ids: list[int] | None = None, http: httpx.Client | None = None,
             mock_url: str | None = None) -> list[InspectionRun]:
    runs = []
    for t in resolve_targets(db, tenant_ids):
        if (types == ["ecs_server"] and trigger == "SCHEDULE"
                and not collection_policy.due(db, t.tenant_id, t.project_ref, "inventory")):
            continue
        try:
            client = t.client(http=http, mock_url=mock_url)
        except HcsError:
            client = None
        runs.append(sync_ledger(db, client=client, target=t, types=types, trigger=trigger))
    return runs
