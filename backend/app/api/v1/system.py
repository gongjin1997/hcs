"""系统管理：平台信息、巡检任务（周期 / crontab / 启停、立即执行）、采集开关、系统设置、平台状态。

- 巡检任务：task:view 查看，task:run 立即执行，task:edit 修改周期与启停（调度进程最迟 30 秒生效，无需重启）
- 采集开关：按资源类型（全局 + 每个租户单独设置）启停台账同步和巡检，collect:manage；部分租户用户只能改自己租户的设置
- 系统设置：数据保留 / 平台自身告警（setting:manage）、定时报表（report:config）、台账字典（ledger:edit）
"""
from datetime import timedelta

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import __version__
from app.collectors.registry import TASKS
from app.core.audit import Auditor, get_auditor
from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import CurrentUser, Scope, get_scope, require_perm
from app.core.events import broadcaster
from app.core.response import BizError, ErrorCode, not_found, ok
from app.ledger.sync import LEDGER_TASK
from app.metrics import CAPABILITIES, SOURCES
from app.models import AlarmEvent, InspectionRun, InspectionTask, Notification, WorkerHeartbeat
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow
from app.schemas.alarm import AlarmOut
from app.services import collect_switch, pipeline, platform_watch, settings_service, silence_service
from app.services.cleanup import CLEANUP_TASK
from app.services.tenancy import tenant_map

router = APIRouter(prefix="/system", tags=["系统管理"])
STARTED_AT = utcnow()
# 系统设置：键 → (查看权限, 修改权限)
SETTING_PERMS = {"retention": ("setting:manage", "setting:manage"), "platform_alarm": ("setting:manage", "setting:manage"),
                 "report": ("report:view", "report:config"), "ledger_dict": ("ledger:view", "ledger:edit")}
MIN_INTERVAL = {LEDGER_TASK: 600}
TASK_NOTES = {
    LEDGER_TASK: "同步 14 类资源的台账（新增 / 变更 / 释放），关闭采集的资源类型会跳过",
    CLEANUP_TASK: "按“系统设置 → 数据保留”删除过期的运行记录、通知、已恢复告警、审计、变更记录等",
    "cce_pod_events": "采集 CCE 集群 Kubernetes Events，汇总容器重启 / OOM / 背压等事件",
}


@router.get("/info", summary="平台信息（适配的 HCS 版本、指标来源说明）")
def info(_: object = Depends(require_perm("task:view", "tenant:view", any_of=True))):
    s = get_settings()
    return ok({"version": __version__, "hcs_version": s.hcs_version, "env": s.env, "metrics_sources": SOURCES,
               "capabilities": CAPABILITIES, "mock": bool(s.hcs_mock_url) and s.env != "prod",
               "notes": {
                   "ces": "HCS 8.5.1 的云监控 CES 为可选组件：选“自动探测”时首次巡检 / 连通性测试会调用 CES 接口，"
                          "未部署（404 / 域名无法解析）时自动降级为 Prometheus 或只巡检状态",
                   "prometheus": "可选：租户已有 Prometheus（node_exporter）时填写地址，作为 CES 的替代或补充",
                   "es": "不需要 Elasticsearch：事件 / 告警 / 台账均为结构化数据，PostgreSQL + 索引 + 定期清理足够"}})


# ---------------- 巡检任务 ----------------
class TaskPatch(BaseModel):
    interval_sec: int | None = Field(None, ge=30, le=7 * 86400)
    cron: str | None = None                  # 空字符串 = 改回按周期执行
    enabled: bool | None = None


def _next_runs(db: Session) -> dict[str, str | None]:
    hb = db.get(WorkerHeartbeat, "scheduler")
    return dict((hb.info or {}).get("jobs") or {}) if hb else {}


def _tenant_name(tm: dict, tid: int | None) -> str | None:
    return (tm[tid].short_name or tm[tid].name) if tid in tm else None


def _skipped(db: Session, code: str, scope: Scope, tm: dict) -> list[str]:
    if code in (CLEANUP_TASK,):
        return []
    return [t.label for t in pipeline.skipped_targets(db, code, scope.tenant_ids)]


@router.get("/tasks", summary="巡检任务与最近运行（每个租户一条运行记录）")
def tasks(db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
          _: object = Depends(require_perm("task:view"))):
    tm = tenant_map(db)
    nxt = _next_runs(db)
    out = []
    for t in db.scalars(select(InspectionTask).order_by(InspectionTask.code)):
        q = scope.apply(select(InspectionRun).where(InspectionRun.task_code == t.code), InspectionRun.tenant_id,
                        include_global=True)
        runs = db.scalars(q.order_by(InspectionRun.started_at.desc()).limit(12)).all()
        out.append({"code": t.code, "name": t.name, "service": t.service, "interval_sec": t.interval_sec,
                    "cron": t.cron, "enabled": t.enabled, "params": t.params, "next_run": nxt.get(t.code),
                    "types": collect_switch.TASK_TYPES.get(t.code, []), "note": TASK_NOTES.get(t.code),
                    "min_interval": MIN_INTERVAL.get(t.code, 30), "skipped_tenants": _skipped(db, t.code, scope, tm),
                    "runs": [{"id": r.id, "tenant_id": r.tenant_id, "tenant_name": _tenant_name(tm, r.tenant_id),
                              "trigger_type": r.trigger_type, "started_at": r.started_at,
                              "finished_at": r.finished_at, "status": r.status, "resource_total": r.resource_total,
                              "abnormal_total": r.abnormal_total, "api_calls": r.api_calls,
                              "error_summary": r.error_summary, "stats": r.stats or {}} for r in runs]})
    return ok(out)


@router.put("/tasks/{code}", summary="修改任务：周期（秒）/ crontab / 启停（调度进程最迟 30 秒生效）")
def update_task(code: str, body: TaskPatch, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                _: CurrentUser = Depends(require_perm("task:edit"))):
    t = db.get(InspectionTask, code)
    if t is None:
        raise not_found("巡检任务")
    data = body.model_dump(exclude_unset=True)
    before = {"interval_sec": t.interval_sec, "cron": t.cron, "enabled": t.enabled}
    if data.get("interval_sec") is not None:
        low = MIN_INTERVAL.get(code, 30)
        if data["interval_sec"] < low:
            raise BizError(ErrorCode.BAD_REQUEST, f"{t.name}的周期不能小于 {low} 秒")
        t.interval_sec = data["interval_sec"]
    if "cron" in data:
        expr = " ".join((data["cron"] or "").split()) or None
        if expr:
            try:
                CronTrigger.from_crontab(expr)
            except ValueError as e:
                raise BizError(ErrorCode.BAD_REQUEST, f"crontab 表达式不正确：{expr}（格式：分 时 日 月 周）") from e
        elif code == CLEANUP_TASK:
            raise BizError(ErrorCode.BAD_REQUEST, "数据清理任务需要 crontab（建议在业务低峰，例如 30 3 * * *）")
        t.cron = expr
    if data.get("enabled") is not None:
        t.enabled = bool(data["enabled"])
    after = {"interval_sec": t.interval_sec, "cron": t.cron, "enabled": t.enabled}
    diff = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    if diff:
        audit.log("task.update", target_type="task", target_id=code, target_name=t.name, detail={"changes": diff})
    else:
        db.commit()
    return ok({**after, "code": code, "changed": bool(diff)})


@router.post("/tasks/{code}/run", summary="立即执行（按当前租户范围逐个租户执行；关闭采集的租户跳过）")
def run_task(code: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
             audit: Auditor = Depends(get_auditor), _: object = Depends(require_perm("task:run"))):
    task = db.get(InspectionTask, code)
    if task is None:
        raise not_found("巡检任务")
    if code == CLEANUP_TASK and not scope.full:
        raise BizError(ErrorCode.FORBIDDEN, "数据清理为全局任务，需要全部租户的数据权限", 403)
    if code not in TASKS and code not in (LEDGER_TASK, CLEANUP_TASK):
        raise BizError(ErrorCode.BAD_REQUEST, f"任务没有对应的采集器：{code}")
    tm = tenant_map(db)
    skipped = _skipped(db, code, scope, tm)
    runs = pipeline.run_all(db, code, trigger="MANUAL", tenant_ids=scope.tenant_ids)
    if not runs and skipped:
        raise BizError(ErrorCode.CONFLICT, f"当前范围内的租户都已关闭该任务的采集：{'、'.join(skipped)}", 409,
                       {"skipped": skipped})
    items = [{"run_id": r.id, "tenant_id": r.tenant_id, "tenant_name": _tenant_name(tm, r.tenant_id),
              "status": r.status, "resource_total": r.resource_total, "abnormal_total": r.abnormal_total,
              "api_calls": r.api_calls, "error_summary": r.error_summary, "stats": r.stats or {}} for r in runs]
    failed = [i for i in items if i["status"] == "FAILED"]
    audit.log("task.run", target_type="task", target_id=code, target_name=task.name,
              tenant_id=scope.single, result="FAILED" if runs and len(failed) == len(runs) else "SUCCESS",
              detail={"runs": [{"tenant": i["tenant_name"], "status": i["status"]} for i in items], "skipped": skipped})
    if runs and len(failed) == len(runs):
        raise BizError(ErrorCode.HCS_UPSTREAM, "执行失败：" + "；".join(
            f"{i['tenant_name'] or '默认'}：{i['error_summary']}" for i in failed)[:500], 502, {"runs": items})
    return ok({"runs": items, "skipped": skipped, "status": "PARTIAL" if failed else "SUCCESS"})


# ---------------- 采集开关 ----------------
class SwitchItem(BaseModel):
    resource_type: str
    tenant_id: int = 0                     # 0 = 全局默认
    enabled: bool | None = None            # 租户设置为 None = 跟随全局


class SwitchesIn(BaseModel):
    items: list[SwitchItem] = Field(min_length=1, max_length=500)


def _check_switches(db: Session, scope: Scope, body: SwitchesIn) -> list[dict]:
    known = set(tenant_map(db))
    items = []
    for it in body.items:
        if it.resource_type not in collect_switch.SWITCH_TYPES:
            raise BizError(ErrorCode.BAD_REQUEST, f"未知资源类型：{it.resource_type}")
        if it.tenant_id == collect_switch.GLOBAL:
            if not scope.full:
                raise BizError(ErrorCode.FORBIDDEN, "修改全局开关需要全部租户的数据权限，请在租户设置里调整", 403)
            if it.enabled is None:
                raise BizError(ErrorCode.BAD_REQUEST, "全局开关只能是开启或关闭")
        else:
            if it.tenant_id not in known:
                raise BizError(ErrorCode.BAD_REQUEST, f"租户不存在：{it.tenant_id}")
            if not scope.allows(it.tenant_id) and not scope.full:
                raise BizError(ErrorCode.FORBIDDEN, "无权修改其他租户的采集开关", 403)
        items.append(it.model_dump())
    return items


@router.get("/collect-switches", summary="采集开关：每类资源的全局开关、各租户设置与生效状态、资源数、活动告警数")
def get_switches(db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                 _: object = Depends(require_perm("task:view", "collect:manage", any_of=True))):
    data = collect_switch.overview(db, scope.tenant_ids)
    return ok({**data, "task_types": collect_switch.TASK_TYPES, "global_editable": scope.full})


@router.post("/collect-switches/impact", summary="保存前预估影响（停止 / 恢复采集的租户、将自动关闭的活动告警数）")
def switch_impact(body: SwitchesIn, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                  _: CurrentUser = Depends(require_perm("collect:manage"))):
    return ok(collect_switch.impact(db, _check_switches(db, scope, body)))


@router.put("/collect-switches", summary="保存采集开关（关闭后：台账同步跳过、巡检不再执行、活动告警自动关闭）")
def save_switches(body: SwitchesIn, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                  audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(require_perm("collect:manage"))):
    items = _check_switches(db, scope, body)
    res = collect_switch.update(db, items, cu.display_name or cu.username)
    if res["changed"]:
        tm = tenant_map(db)
        label = {True: "开启", False: "关闭", None: "跟随全局"}
        audit.log("collect.update", target_type="collect_switch", target_name=f"{len(res['changed'])} 项采集开关",
                  tenant_id=scope.single,
                  detail={"changes": [f"{_tenant_name(tm, c['tenant_id']) or '全局'} · {c['name']}："
                                      f"{label[c['old']] if c['old'] is not None or c['tenant_id'] else '开启'} → "
                                      f"{label[c['new']]}" for c in res["changed"]],
                          "closed_alarms": res["closed_alarms"]})
    else:
        db.commit()
    return ok(res)


# ---------------- 系统设置 ----------------
def _can(cu: CurrentUser, perm: str) -> bool:
    return cu.can(perm)


def _fields(key: str) -> list[dict]:
    model = settings_service.MODELS[key]
    out = []
    for name, f in model.model_fields.items():
        item: dict = {"key": name, "label": f.description or name, "default": f.default
                      if not callable(f.default) else None}
        for m in f.metadata:
            for attr in ("ge", "le"):
                if hasattr(m, attr):
                    item[attr] = getattr(m, attr)
        out.append(item)
    return out


@router.get("/settings", summary="系统设置（只返回有权限查看的设置项）")
def get_all_settings(db: Session = Depends(get_db), cu: CurrentUser = Depends(require_perm(
        "setting:manage", "report:view", "ledger:view", any_of=True))):
    out = {}
    for key, (view, edit) in SETTING_PERMS.items():
        if _can(cu, view):
            out[key] = {"label": settings_service.LABELS[key], "value": settings_service.get(db, key).model_dump(mode="json"),
                        "editable": _can(cu, edit), "fields": _fields(key), **settings_service.meta(db, key)}
    return ok(out)


@router.put("/settings/{key}", summary="修改系统设置（保存后各进程下次使用时生效，无需重启）")
def put_setting(key: str, body: dict, db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(require_perm(
                    "setting:manage", "report:config", "ledger:edit", any_of=True))):
    if key not in SETTING_PERMS:
        raise not_found("设置项")
    if not _can(cu, SETTING_PERMS[key][1]):
        raise BizError(ErrorCode.FORBIDDEN, f"缺少权限：修改{settings_service.LABELS[key]}", 403)
    if not scope.full:
        raise BizError(ErrorCode.FORBIDDEN, "系统设置为全局配置，修改需要全部租户的数据权限", 403)
    value, diff = settings_service.save(db, key, body, cu.display_name or cu.username)
    if diff:
        audit.log("setting.update", target_type="setting", target_id=key, target_name=settings_service.LABELS[key],
                  detail={"changes": diff})
    else:
        db.commit()
    return ok({"value": value.model_dump(mode="json"), "changed": list(diff), **settings_service.meta(db, key)})


# ---------------- 平台状态 ----------------
def _worker(hb: WorkerHeartbeat | None, name: str, timeout_min: int) -> dict:
    if hb is None:
        return {"name": name, "label": platform_watch.WORKERS[name], "status": "NEVER"}
    age = (utcnow() - hb.beat_at).total_seconds()
    return {"name": name, "label": platform_watch.WORKERS[name], "host": hb.host, "pid": hb.pid, "version": hb.version,
            "started_at": hb.started_at, "beat_at": hb.beat_at, "age_sec": int(age),
            "status": "OK" if age <= timeout_min * 60 else "LATE", "info": hb.info or {}}


@router.get("/platform", summary="平台状态：后台进程心跳、通知投递、任务健康、平台自身告警、数据清理")
def platform(db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
             _: object = Depends(require_perm("task:view"))):
    s = get_settings()
    now = utcnow()
    cfg = settings_service.get_platform_alarm(db)
    workers = [_worker(db.get(WorkerHeartbeat, n), n, cfg.heartbeat_timeout_min) for n in platform_watch.WORKERS]
    day = now - timedelta(hours=24)
    by_status = dict(db.execute(select(Notification.status, func.count()).where(Notification.created_at >= day)
                                .group_by(Notification.status)).all())
    backlog = dict(db.execute(select(Notification.status, func.count()).where(
        Notification.status.in_(("PENDING", "RETRYING"))).group_by(Notification.status)).all())
    oldest = db.scalar(select(func.min(Notification.created_at)).where(Notification.status.in_(("PENDING", "RETRYING"))))
    last_sent = db.scalar(select(func.max(Notification.sent_at)).where(Notification.status == "SENT"))
    tm = tenant_map(db)
    health = []
    for t in db.scalars(select(InspectionTask).order_by(InspectionTask.code)):
        q = scope.apply(select(InspectionRun).where(InspectionRun.task_code == t.code, InspectionRun.status != "RUNNING"),
                        InspectionRun.tenant_id, include_global=True)
        runs = db.scalars(q.order_by(InspectionRun.started_at.desc()).limit(60)).all()
        per: dict = {}
        for r in runs:
            p = per.setdefault(r.tenant_id, {"tenant_id": r.tenant_id, "tenant_name": _tenant_name(tm, r.tenant_id),
                                             "last_status": r.status, "last_at": r.started_at, "fails": 0,
                                             "counting": True, "last_success_at": None})
            if p["counting"] and r.status == "FAILED":
                p["fails"] += 1
            elif r.status != "FAILED":
                p["counting"] = False
                p["last_success_at"] = p["last_success_at"] or r.started_at
        tenants = [{k: v for k, v in p.items() if k != "counting"} for p in per.values()]
        health.append({"code": t.code, "name": t.name, "enabled": t.enabled, "interval_sec": t.interval_sec,
                       "cron": t.cron, "tenants": tenants,
                       "status": "FAILED" if any(p["fails"] >= cfg.task_fail_threshold for p in tenants)
                       else ("WARN" if any(p["fails"] for p in tenants) else "OK")})
    alarms_q = scope.apply(select(AlarmEvent).where(AlarmEvent.service == platform_watch.PLATFORM,
                                                    AlarmEvent.status.in_(ACTIVE_STATUSES)),
                           AlarmEvent.tenant_id, include_global=True)
    alarms = db.scalars(alarms_q.order_by(AlarmEvent.first_seen.desc()).limit(20)).all()
    cleanup = db.scalar(select(InspectionRun).where(InspectionRun.task_code == CLEANUP_TASK)
                        .order_by(InspectionRun.started_at.desc()).limit(1))
    silences = len(silence_service.active_silences(db, now))
    redis_ok = None
    if s.redis_url:
        try:
            import redis

            redis_ok = bool(redis.Redis.from_url(s.redis_url, socket_timeout=1).ping())
        except Exception:  # noqa: BLE001
            redis_ok = False
    return ok({
        "version": __version__, "env": s.env, "timezone": s.timezone, "api_started_at": STARTED_AT,
        "database": db.bind.dialect.name if db.bind is not None else None,
        "redis": {"configured": bool(s.redis_url), "ok": redis_ok}, "sse_subscribers": broadcaster.count,
        "metrics": {"path": "/metrics", "token_required": bool(s.metrics_token)},
        "workers": workers, "heartbeat_timeout_min": cfg.heartbeat_timeout_min,
        "outbox": {"sent_24h": by_status.get("SENT", 0), "failed_24h": by_status.get("FAILED", 0),
                   "merged_24h": by_status.get("MERGED", 0), "pending": backlog.get("PENDING", 0),
                   "retrying": backlog.get("RETRYING", 0), "oldest_pending_at": oldest, "last_sent_at": last_sent},
        "tasks": health, "task_fail_threshold": cfg.task_fail_threshold,
        "platform_alarms": [AlarmOut.model_validate(a) for a in alarms], "active_silences": silences,
        "cleanup": {"started_at": cleanup.started_at, "status": cleanup.status, "stats": cleanup.stats or {},
                    "deleted": cleanup.resource_total} if cleanup else None,
        "platform_alarm": cfg.model_dump(mode="json"),
    })
