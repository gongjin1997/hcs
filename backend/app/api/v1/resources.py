"""资源台账：分类与类型、列表、汇总、变更记录、导出 Excel、手动同步、详情、维护管理字段。全部按租户范围过滤。"""
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, load_only

from app.core.audit import Auditor, get_auditor
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.response import ApiResponse, BizError, ErrorCode, Page, not_found, ok
from app.core.xlsx import filename_stamp, xlsx_response
from app.ledger import importer
from app.ledger.excel import export_ledger
from app.ledger.sync import LEDGER_TASK, _change, record_manual_change, sync_all
from app.models import AlarmEvent, InspectionRun, Resource, ResourceChange
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow
from app.schemas.alarm import AlarmOut
from app.schemas.ecs import EcsInstanceOut
from app.schemas.resource import ChangeOut, ClassificationConfig, ResourceOut, ResourcePatch
from app.services import collect_switch, platform_watch, resource_classification, settings_service
from app.services.capacity_summary import capacity_totals
from app.services.catalog import CATEGORIES, ENV_TYPES, LEDGER_TYPES, RESOURCE_TYPES, category_types, ledger_categories
from app.services.ecs_monitoring import snapshots as ecs_snapshots
from app.services.identity import resolve_owner, user_options
from app.services.inspection_health import current_health
from app.services.metric_visibility import current_metrics
from app.services.tenancy import tenant_map

router = APIRouter(prefix="/resources", tags=["资源台账"])
VIEW = Depends(require_perm("ledger:view"))
HEALTH_ORDER = {"CRITICAL": 0, "MAJOR": 1, "WARNING": 2, "INFO": 3, "UNKNOWN": 4, "OK": 5}


def _active_counts(db: Session, ids: list[int]) -> dict[int, int]:
    rows = db.execute(select(AlarmEvent.resource_ref, func.count()).where(
        AlarmEvent.resource_ref.in_(ids), AlarmEvent.status.in_(ACTIVE_STATUSES)).group_by(AlarmEvent.resource_ref))
    return {int(ref): int(n) for ref, n in rows if ref is not None}


def _distinct(db: Session, col, scope: Scope) -> list[str]:
    """筛选项：在用资源的业务系统 / 负责人去重列表。"""
    q = scope.apply(select(col).where(Resource.lifecycle == "ACTIVE", col.is_not(None)), Resource.tenant_id)
    return sorted(v for v in db.scalars(q.distinct()) if v)


def _get(db: Session, scope: Scope, rid: int) -> Resource:
    r = db.get(Resource, rid)
    if r is None:
        raise not_found("资源")
    scope.ensure(r.tenant_id, "资源")
    return r


def _owner_options(db: Session, scope: Scope) -> list[dict]:
    return [u for u in user_options(db, scope.tenant_ids) if u["enabled"] and u["tenant_ids"] != []]


def _resource_out(resource: Resource, config: ClassificationConfig, active_alarms: int = 0,
                  ecs: EcsInstanceOut | None = None) -> ResourceOut:
    category, source = resource_classification.classify(resource, config)
    changes: dict[str, object] = {"classification": category, "classification_source": source, "active_alarms": active_alarms}
    changes["metrics"] = current_metrics(resource, ecs)
    changes["health"] = current_health(resource)
    if resource.resource_type == "ecs_server":
        if ecs is not None:
            changes["attrs"] = {**(resource.attrs or {}), "metrics_source": ecs.metrics_source,
                                "ecs_latest": ecs.model_dump(mode="json")["latest"],
                                "ecs_diagnostic": ecs.diagnostic.model_dump(mode="json")}
    return ResourceOut.model_validate(resource).model_copy(update=changes)


def _types_for(resource_type: str | None, category: str | None) -> list[str] | None:
    if resource_type:
        if resource_type not in RESOURCE_TYPES:
            raise BizError(ErrorCode.BAD_REQUEST, f"未知资源类型：{resource_type}")
        return [resource_type]
    if category == "ledger":
        return LEDGER_TYPES
    return category_types(category) if category else None


@router.get("/types", summary="台账分类与资源类型（含数量、异常数、列定义）", dependencies=[VIEW])
def resource_types(resource_type: str | None = None, category: str | None = None,
                   db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    classification_types = _types_for(resource_type, category)
    active = Resource.lifecycle == "ACTIVE"
    counts = dict(db.execute(scope.apply(select(Resource.resource_type, func.count()).where(active), Resource.tenant_id)
                             .group_by(Resource.resource_type)).all())
    abnormal = dict(db.execute(scope.apply(select(Resource.resource_type, func.count()).where(
        active, Resource.health.notin_(("OK", "UNKNOWN"))), Resource.tenant_id).group_by(Resource.resource_type)).all())
    sw = collect_switch.load(db)
    tenants = [t for t in tenant_map(db).values() if scope.allows(t.id)]

    def collect(code: str) -> dict:
        off = [t.short_name or t.name for t in tenants if not sw.enabled(t.id, code)]
        return {"enabled": len(off) < len(tenants) or not tenants, "off_tenants": off, "global": sw.enabled(None, code)}

    types = [{"code": k, "name": v["name"], "category": v["category"], "service": v["service"],
              "ledger": v["ledger"], "inspected": "group" in v, "columns": v["columns"],
              "total": counts.get(k, 0), "abnormal": abnormal.get(k, 0), "collect": collect(k)}
             for k, v in RESOURCE_TYPES.items()]
    cats = [{**c, "total": sum(t["total"] for t in types if t["category"] == c["code"]),
             "abnormal": sum(t["abnormal"] for t in types if t["category"] == c["code"])} for c in CATEGORIES]
    d = settings_service.get_ledger_dict(db)
    return ok({"categories": cats, "types": types, "env_types": ENV_TYPES,
               "classifications": resource_classification.summary(db, resource_classification.load(db),
                   tenant_ids=scope.tenant_ids, resource_types=classification_types),
               "options": {"business": _distinct(db, Resource.business, scope),
                           "owner": _distinct(db, Resource.owner, scope)},
               "dict": {"business": [i.name for i in d.business],
                        "owners": sorted({u["display_name"] for u in _owner_options(db, scope)}),
                        "enforce": d.enforce}})


@router.get("", response_model=ApiResponse[Page[ResourceOut]], summary="台账列表", dependencies=[VIEW])
def list_resources(resource_type: str | None = None, category: str | None = None,
                   health: list[str] | None = Query(None), keyword: str | None = None, owner: str | None = None,
                   business: str | None = None, env_type: str | None = None, critical: bool | None = None,
                   classification: str | None = Query(None, min_length=1, max_length=64),
                   unassigned: bool = False, lifecycle: str = Query("ACTIVE", pattern="^(ACTIVE|DELETED|ALL)$"),
                   status: list[str] | None = Query(None), page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=500), db: Session = Depends(get_db),
                   scope: Scope = Depends(get_scope)):
    q = scope.apply(select(Resource), Resource.tenant_id)
    types = _types_for(resource_type, category)
    config = resource_classification.load(db)
    if classification is not None:
        q = q.where(resource_classification.expression(db, config, tenant_ids=scope.tenant_ids,
                                                       resource_types=types) == classification)
    if status:
        q = q.where(Resource.status_raw.in_(status))
    if types is not None:
        q = q.where(Resource.resource_type.in_(types))
    if lifecycle != "ALL":
        q = q.where(Resource.lifecycle == lifecycle)
    if critical is not None:
        q = q.where(Resource.critical.is_(critical))
    if owner:
        q = q.where(Resource.owner == owner)
    if business:
        q = q.where(Resource.business == business)
    if env_type:
        q = q.where(Resource.env_type == env_type)
    if unassigned:
        q = q.where(Resource.owner.is_(None))
    if keyword:
        like = f"%{keyword.strip()}%"
        q = q.where(or_(Resource.name.ilike(like), Resource.resource_id.ilike(like), Resource.ip.ilike(like),
                        Resource.owner.ilike(like), Resource.business.ilike(like)))
    if health:
        # 只有显式健康筛选才扫描轻量证据列；不能用过期的持久化 OK 命中“正常”。
        now = utcnow()
        candidates = []
        for row in db.scalars(q.options(load_only(Resource.id, Resource.health, Resource.attrs, Resource.monitored,
                Resource.lifecycle, Resource.critical, Resource.name)).execution_options(yield_per=500)):
            state = current_health(row, now)
            if state in health:
                candidates.append((HEALTH_ORDER.get(state, 9), not row.critical, row.name, row.id))
        candidates.sort()
        total = len(candidates)
        ids = [entry[3] for entry in candidates[(page - 1) * page_size:page * page_size]]
        found = {row.id: row for row in db.scalars(q.where(Resource.id.in_(ids)))} if ids else {}
        rows = [found[rid] for rid in ids]
    else:
        total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
        rows = list(db.scalars(q.order_by(case(HEALTH_ORDER, value=Resource.health, else_=9), Resource.critical.desc(),
                                     Resource.name, Resource.id).offset((page - 1) * page_size).limit(page_size)))
    counts = _active_counts(db, [r.id for r in rows])
    snapshots = ecs_snapshots(db, rows)
    items = [_resource_out(r, config, counts.get(r.id, 0), snapshots.get(r.id)) for r in rows]
    return ok(Page(items=items, total=total, page=page, page_size=page_size))


@router.get("/summary", summary="台账汇总（资源数、算力 / 存储合计、闲置、近 7 天变更、最近同步）", dependencies=[VIEW])
def ledger_summary(db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    now = utcnow()
    rows = db.scalars(scope.apply(select(Resource).where(Resource.lifecycle == "ACTIVE",
                                                         Resource.resource_type.in_(LEDGER_TYPES)),
                                  Resource.tenant_id)).all()
    changes = dict(db.execute(scope.apply(select(ResourceChange.change_type, func.count()).where(
        ResourceChange.detected_at >= now - timedelta(days=7)), ResourceChange.tenant_id)
        .group_by(ResourceChange.change_type)).all())
    last = db.scalar(scope.apply(select(InspectionRun).where(InspectionRun.task_code == LEDGER_TASK),
                                 InspectionRun.tenant_id, include_global=True)
                     .order_by(InspectionRun.started_at.desc()).limit(1))
    ecs = [r for r in rows if r.resource_type == "ecs_server"]
    capacity = capacity_totals(rows)
    idle = {"evs_available": sum(1 for r in rows if r.resource_type == "evs_volume" and r.status_raw == "available"),
            "eip_unbound": sum(1 for r in rows if r.resource_type == "eip" and r.status_raw == "DOWN"),
            "ecs_stopped": sum(1 for r in ecs if r.status_raw == "SHUTOFF")}
    by_cat = [{"code": c["code"], "name": c["name"],
               "total": sum(1 for r in rows if RESOURCE_TYPES[r.resource_type]["category"] == c["code"])}
              for c in ledger_categories()]
    return ok({
        "total": len(rows), "type_count": len({r.resource_type for r in rows}), "by_category": by_cat,
        "compute": {"ecs": len(ecs), "vcpus": capacity["vcpus"], "memory_gb": capacity["memory_gb"]},
        "storage_gb": capacity["storage_gb"], "capacity_coverage": capacity["coverage"],
        "unassigned": sum(1 for r in rows if not r.owner), "critical": sum(1 for r in rows if r.critical),
        "idle": idle, "changes_7d": {k: changes.get(k, 0) for k in ("CREATED", "UPDATED", "DELETED", "STATUS",
                                                                     "REBOOT")},
        "ecs_status": {k: sum(1 for r in ecs if (r.status_raw or "UNKNOWN") == k) for k in ("ACTIVE", "SHUTOFF",
                                                                                             "ERROR")},
        "last_sync": {"started_at": last.started_at, "status": last.status, "api_calls": last.api_calls,
                      "duration_sec": round((last.finished_at - last.started_at).total_seconds(), 1)
                      if last.finished_at else None} if last else None,
    })


@router.get("/changes", response_model=ApiResponse[Page[ChangeOut]], summary="台账变更记录（含状态变更、重启）",
            dependencies=[VIEW])
def list_changes(days: int = Query(30, ge=1, le=365), change_type: list[str] | None = Query(None),
                 resource_type: str | None = None, category: str | None = None, keyword: str | None = None,
                 source: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200),
                 db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    q = scope.apply(select(ResourceChange).where(ResourceChange.detected_at >= utcnow() - timedelta(days=days)),
                    ResourceChange.tenant_id)
    types = _types_for(resource_type, category)
    if types is not None:
        q = q.where(ResourceChange.resource_type.in_(types))
    if change_type:
        q = q.where(ResourceChange.change_type.in_(change_type))
    if source:
        q = q.where(ResourceChange.source == source)
    if keyword:
        like = f"%{keyword.strip()}%"
        q = q.where(or_(ResourceChange.name.ilike(like), ResourceChange.resource_id.ilike(like)))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(ResourceChange.detected_at.desc(), ResourceChange.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return ok(Page(items=[ChangeOut.model_validate(r) for r in rows], total=total, page=page, page_size=page_size))


@router.get("/export", summary="导出台账 Excel（汇总页 + 每类资源一页）")
def export(resource_type: str | None = None, category: str | None = None,
           classification: str | None = Query(None, min_length=1, max_length=64), db: Session = Depends(get_db),
           scope: Scope = Depends(get_scope), audit: Auditor = Depends(get_auditor),
           _: CurrentUser = Depends(require_perm("ledger:export"))):
    types = [t for t in (_types_for(resource_type, category) or LEDGER_TYPES) if RESOURCE_TYPES[t]["ledger"]]
    if not types:
        raise BizError(ErrorCode.BAD_REQUEST, "作业 / 容器工作负载不在台账范围内")
    data = export_ledger(db, types, tenant_ids=scope.tenant_ids, classification=classification)
    audit.log("ledger.export", target_type="ledger", target_name=resource_type or category or "全部资源",
              tenant_id=scope.single, detail={"types": types, "classification": classification})
    return xlsx_response(data, f"资源台账_{filename_stamp(utcnow(), '%Y%m%d_%H%M')}.xlsx")


@router.post("/sync", summary="立即同步台账（按当前租户范围；可只同步某一类）")
def sync_now(resource_type: str | None = None, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
             audit: Auditor = Depends(get_auditor), _: CurrentUser = Depends(require_perm("ledger:sync"))):
    if resource_type and not RESOURCE_TYPES.get(resource_type, {}).get("ledger"):
        raise BizError(ErrorCode.BAD_REQUEST, f"不支持同步的资源类型：{resource_type}")
    runs = sync_all(db, types=[resource_type] if resource_type else None, trigger="MANUAL",
                    tenant_ids=scope.tenant_ids)
    tm = tenant_map(db)
    items = [{"run_id": r.id, "tenant_id": r.tenant_id, "project_ref": r.project_ref,
              "tenant_name": (tm[r.tenant_id].short_name or tm[r.tenant_id].name) if r.tenant_id in tm else None,
              "status": r.status, "resource_total": r.resource_total, "changes": r.abnormal_total,
              "stats": r.stats, "errors": r.error_summary} for r in runs]
    status = platform_watch.run_outcome(runs)
    failed = [i for i in items if i["status"] in ("FAILED", "TIMEOUT")]
    audit.log("ledger.sync", target_type="ledger", target_name=RESOURCE_TYPES.get(resource_type or "", {}).get(
        "name", "全部资源"), tenant_id=scope.single, result="SUCCESS" if status == "SUCCESS" else "FAILED",
        detail={"outcome": status, "runs": [{"tenant": i["tenant_name"], "project_ref": i["project_ref"],
                 "status": i["status"], "changes": i["changes"]} for i in items]})
    if status == "FAILED":
        raise BizError(ErrorCode.HCS_UPSTREAM, "同步失败：" + "；".join(
            f"{i['tenant_name'] or '默认'}：{i['errors']}" for i in failed)[:500], 502, {"runs": items})
    return ok({"runs": items, "status": status,
               "resource_total": sum(i["resource_total"] for i in items),
               "changes": sum(i["changes"] for i in items)})


# ---------------- 台账字典 / Excel 批量导入 ----------------
@router.get("/dict", summary="业务系统字典与只读负责人目录（含使用次数与历史取值）", dependencies=[VIEW])
def get_dict(db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    d = settings_service.get_ledger_dict(db)
    out: dict = {"enforce": d.enforce, **settings_service.meta(db, "ledger_dict")}
    for key, col in (("business", Resource.business),):
        used = dict(db.execute(scope.apply(select(col, func.count()).where(Resource.lifecycle == "ACTIVE",
                                                                           col.is_not(None)), Resource.tenant_id)
                               .group_by(col)).all())
        items = getattr(d, key)
        names = {i.name for i in items}
        out[key] = [{**i.model_dump(), "used": used.get(i.name, 0)} for i in items]
        out[f"{key}_outside"] = sorted(({"name": k, "used": v} for k, v in used.items() if k and k not in names),
                                       key=lambda x: -x["used"])
    owners = _owner_options(db, scope)
    used_owners = db.execute(scope.apply(select(Resource.owner_user_id, Resource.owner, func.count()).where(
        Resource.lifecycle == "ACTIVE"), Resource.tenant_id).group_by(Resource.owner_user_id, Resource.owner)).all()
    used_ids: dict[int, int] = {}
    outside: dict[str, int] = {}
    eligible = {u["id"] for u in owners}
    for uid, name, count in used_owners:
        if uid in eligible:
            used_ids[uid] = used_ids.get(uid, 0) + count
        elif name:
            outside[name] = outside.get(name, 0) + count
    out["owners"] = [{"id": u["id"], "username": u["username"], "name": u["display_name"],
                      "remark": None, "used": used_ids.get(u["id"], 0)} for u in owners]
    out["owners_outside"] = sorted(({"name": name, "used": count} for name, count in outside.items()),
                                   key=lambda x: -x["used"])
    return ok(out)


def _derived_owners(value, derived: list[dict]) -> bool:
    """兼容旧客户端回传目录或 name/remark 投影，不允许在字典里维护人员。"""
    if not isinstance(value, list) or len(value) != len(derived):
        return False
    remaining = list(derived)
    keys = {"id", "username", "name", "remark"}
    for item in value:
        if not isinstance(item, dict) or "name" not in item:
            return False
        match = next((entry for entry in remaining if all(
            item[key] == entry[key] for key in keys & item.keys())), None)
        if match is None:
            return False
        remaining.remove(match)
    return True


@router.put("/dict", summary="保存业务系统字典（负责人统一在用户管理维护）")
def put_dict(body: dict, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
             audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(require_perm("ledger:edit"))):
    if not scope.full:
        raise BizError(ErrorCode.FORBIDDEN, "台账字典为全局配置，修改需要全部租户的数据权限", 403)
    owners = get_dict(db, scope)["data"]["owners"]
    if "owners" in body and not _derived_owners(body["owners"], owners):
        raise BizError(ErrorCode.BAD_REQUEST, "负责人由用户目录统一提供，请到用户管理维护人员，不能修改负责人字典")
    value, diff = settings_service.save(db, "ledger_dict", {k: v for k, v in body.items() if k != "owners"},
                                       cu.display_name or cu.username)
    if diff:
        audit.log("ledger.dict", target_type="setting", target_id="ledger_dict", target_name="台账字典",
                  detail={"changes": {k: v if k == "enforce" else [len(v[0] or []), len(v[1] or [])]
                                      for k, v in diff.items()}})
    else:
        db.commit()
    return ok({**value.model_dump(mode="json"), "owners": owners})


@router.get("/import/template", summary="下载导入模板（按筛选导出在用资源，已带当前取值）")
def import_template(resource_type: str | None = None, category: str | None = None, db: Session = Depends(get_db),
                    scope: Scope = Depends(get_scope), _: CurrentUser = Depends(require_perm("ledger:edit"))):
    types = [t for t in (_types_for(resource_type, category) or LEDGER_TYPES) if RESOURCE_TYPES[t]["ledger"]]
    data = importer.template(db, types, scope.tenant_ids)
    return xlsx_response(data, f"台账管理字段导入模板_{filename_stamp(utcnow(), '%Y%m%d_%H%M')}.xlsx")


@router.post("/import", summary="Excel 批量导入管理字段（dry_run=true 只预览；确认后 dry_run=false 写入）")
def import_ledger(file: UploadFile = File(...), dry_run: bool = Query(True),
                        skip_errors: bool = Query(False, description="有错误行时跳过错误行，只导入正确的行"),
                        db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                        audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(require_perm("ledger:edit"))):
    content = file.file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise BizError(ErrorCode.BAD_REQUEST, "文件不能超过 10 MB")
    res, todo = importer.parse(db, content, scope.tenant_ids, settings_service.get_ledger_dict(db))
    out = res.as_dict()
    if dry_run:
        return ok({**out, "dry_run": True})
    if res.errors and not skip_errors:
        raise BizError(ErrorCode.BAD_REQUEST, f"有 {len(res.errors)} 行校验未通过，请修改后重新上传或选择跳过错误行", 400, out)
    n = importer.apply(db, todo, cu.display_name or cu.username)
    audit.log("ledger.import", target_type="ledger", target_name=file.filename or "Excel", tenant_id=scope.single,
              detail={"rows": res.total, "changed": n, "skipped_errors": len(res.errors),
                      "resources": [i["name"] for i in res.items[:50]]})
    return ok({**out, "dry_run": False, "applied": n})


@router.get("/classifications", summary="资源分类映射规则及当前租户范围的在用资源分类数量", dependencies=[VIEW])
def get_classifications(resource_type: str | None = None, category: str | None = None,
                        db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    types = _types_for(resource_type, category)
    config = resource_classification.load(db)
    return ok({**config.model_dump(mode="json"), **resource_classification.summary(
        db, config, tenant_ids=scope.tenant_ids, resource_types=types),
        **settings_service.meta(db, resource_classification.KEY)})


@router.put("/classifications", summary="保存全局资源分类映射（按顺序首次命中，不修改业务系统或资源类型）")
def put_classifications(body: ClassificationConfig, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                        audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(require_perm("ledger:edit"))):
    if not scope.full:
        raise BizError(ErrorCode.FORBIDDEN, "资源分类映射为全局配置，修改需要全部租户的数据权限", 403)
    diff = resource_classification.save(db, body, cu.display_name or cu.username)
    audit.log("ledger.classification", target_type="setting", target_id=resource_classification.KEY,
              target_name="资源分类映射", detail={"changes": diff})
    return get_classifications(db=db, scope=scope)


@router.get("/{rid}", summary="资源详情（台账信息 + 告警 + 变更记录 + 状态时间线）", dependencies=[VIEW])
def get_resource(rid: int, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    r = _get(db, scope, rid)
    alarms = db.scalars(select(AlarmEvent).where(AlarmEvent.resource_ref == rid)
                        .order_by(AlarmEvent.first_seen.desc()).limit(20)).all()
    changes = db.scalars(select(ResourceChange).where(ResourceChange.resource_ref == rid,
                                                      ResourceChange.change_type.notin_(("STATUS", "REBOOT")))
                         .order_by(ResourceChange.detected_at.desc()).limit(30)).all()
    status = db.scalars(select(ResourceChange).where(ResourceChange.resource_ref == rid,
                                                     ResourceChange.change_type.in_(("STATUS", "REBOOT")),
                                                     ResourceChange.detected_at >= utcnow() - timedelta(days=30))
                        .order_by(ResourceChange.detected_at.desc()).limit(50)).all()
    out = _resource_out(r, resource_classification.load(db), sum(1 for a in alarms if a.status in ACTIVE_STATUSES),
                        ecs_snapshots(db, [r]).get(r.id))
    tm = tenant_map(db)
    t = tm.get(r.tenant_id or 0)
    return ok({"resource": out, "alarms": [AlarmOut.model_validate(a) for a in alarms],
               "changes": [ChangeOut.model_validate(c) for c in changes],
               "status_changes": [ChangeOut.model_validate(c) for c in status],
               "tenant": {"id": t.id, "name": t.name, "short_name": t.short_name or t.name, "color": t.color}
               if t else None})


@router.patch("/{rid}", response_model=ApiResponse[ResourceOut],
              summary="维护管理字段（负责人 / 业务系统 / 环境 / 备注 / 核心 / 预期状态）")
def patch_resource(rid: int, body: ResourcePatch, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                   audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(require_perm("ledger:edit"))):
    r = _get(db, scope, rid)
    data = body.model_dump(exclude_unset=True)
    if "env_type" in data and data["env_type"] not in (None, *ENV_TYPES):
        raise BizError(ErrorCode.BAD_REQUEST, f"环境取值：{'/'.join(ENV_TYPES)}")
    d = settings_service.get_ledger_dict(db)
    if d.enforce:                                  # 台账字典：只能从字典中选择
        for key, items, label in (("business", d.business, "业务系统"),):
            v = (data.get(key) or "").strip() if isinstance(data.get(key), str) else data.get(key)
            if v and v != getattr(r, key) and v not in {i.name for i in items}:
                raise BizError(ErrorCode.BAD_REQUEST, f"{label}“{v}”不在台账字典中（已开启只能从字典选择）")
    if "owner_user_id" in data:
        owner = resolve_owner(db, tenant_id=r.tenant_id, owner_user_id=data["owner_user_id"])
        data.update(owner_user_id=owner.id if owner else None, owner=owner.display_name if owner else None)
    elif "owner" in data:
        name = (data["owner"] or "").strip() or None
        if name is None or name != r.owner:
            owner = resolve_owner(db, tenant_id=r.tenant_id, owner=name)
            data.update(owner_user_id=owner.id if owner else None, owner=owner.display_name if owner else None)
    expected = data.pop("expected_state", None)
    before = {k: getattr(r, k) for k in data}
    for k, v in data.items():
        setattr(r, k, (v.strip() or None) if isinstance(v, str) else v)
    record_manual_change(db, r, before, cu.display_name)
    detail = {k: [before[k], getattr(r, k)] for k in data if before[k] != getattr(r, k)}
    if expected is not None and "running" in expected:
        old_run = (r.expected_state or {}).get("running", True)
        new_run = bool(expected["running"])
        if old_run != new_run:
            r.expected_state = {**(r.expected_state or {}), "running": new_run}
            run_label = {True: "应保持运行", False: "允许关机"}
            _change(db, r, "UPDATED", utcnow(), [{"field": "expected_state.running", "label": "预期状态",
                                                  "old": run_label[old_run], "new": run_label[new_run]}],
                    source="MANUAL", operator=cu.display_name)
            detail["预期状态"] = [run_label[old_run], run_label[new_run]]
    if detail:
        audit.log("ledger.update", target_type="resource", target_id=r.id, target_name=r.name, tenant_id=r.tenant_id,
                  detail={"changes": detail})
    else:
        db.commit()
    return ok(_resource_out(r, resource_classification.load(db), ecs=ecs_snapshots(db, [r]).get(r.id)))
