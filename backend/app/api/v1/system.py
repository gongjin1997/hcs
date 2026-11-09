"""系统管理：平台信息、巡检任务（周期 / crontab / 启停、立即执行）、采集开关、系统设置、平台状态。

- 巡检任务：task:view 查看，task:run 立即执行，task:edit 修改周期与启停（调度进程最迟 30 秒生效，无需重启）
- 采集开关：按资源类型（全局 + 每个租户单独设置）启停台账同步和巡检，collect:manage；部分租户用户只能改自己租户的设置
- 系统设置：数据保留 / 平台自身告警（setting:manage）、定时报表（report:config）、台账字典（ledger:edit）
"""
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
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
from app.schemas.collect_scope import ScopeIn
from app.schemas.job_selection import JobResourceType, JobSelectionIn
from app.schemas.system import TaskRunOut, TaskScopeOut
from app.services import (
    catalog,
    collect_scope,
    collect_switch,
    inspection_evidence,
    job_selection,
    pipeline,
    platform_watch,
    settings_service,
    silence_service,
)
from app.services.cleanup import CLEANUP_TASK
from app.services.tenancy import tenant_map

router = APIRouter(prefix="/system", tags=["系统管理"])
STARTED_AT = utcnow()
log = logging.getLogger(__name__)
_redis_health_lock = threading.Lock()
_redis_failed = False
_redis_next_log = 0.0


def _redis_health(url: str) -> bool:
    global _redis_failed, _redis_next_log
    failure = None
    try:
        import redis

        with redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1) as client:
            healthy = bool(client.ping())
    except Exception as exc:
        failure, healthy = exc, False
    with _redis_health_lock:
        now = time.monotonic()
        if not healthy and (not _redis_failed or now >= _redis_next_log):
            safe = RuntimeError(type(failure).__name__ if failure else "Redis ping failed")
            log.warning("dependency.failed", extra={"dependency": "redis", "exception_type": type(failure).__name__},
                        exc_info=(RuntimeError, safe, failure.__traceback__) if failure else None)
            _redis_next_log = now + 60
        elif healthy and _redis_failed:
            log.info("dependency.restored", extra={"dependency": "redis"})
        _redis_failed = not healthy
    return healthy


# 系统设置：键 → (查看权限, 修改权限)
SETTING_PERMS = {"retention": ("setting:manage", "setting:manage"), "platform_alarm": ("setting:manage", "setting:manage"),
                 "report": ("report:view", "report:config"), "ledger_dict": ("ledger:view", "ledger:edit"),
                 "collect": ("setting:manage", "setting:manage")}
MIN_INTERVAL = {LEDGER_TASK: 600}
TASK_NOTES = catalog.TASK_NOTES   # 任务说明与任务定义同处 catalog.TASK_SPECS
AOM_MAPPING_FIELDS = {"namespace", "dimension", "metrics", "units", "period", "window_sec", "prometheus_instance_id"}


@router.get("/info", summary="平台信息（适配的 HCS 版本、指标来源说明）")
def info(_: object = Depends(require_perm("task:view", "tenant:view", any_of=True))):
    s = get_settings()
    return ok({"version": __version__, "hcs_version": s.hcs_version, "env": s.env, "metrics_sources": SOURCES,
               "capabilities": CAPABILITIES, "ces_enabled": s.hcs_ces_enabled,
               "mock": bool(s.hcs_mock_url) and s.env != "prod",
               "notes": {
                   "aom": "AOM 已全局停用，所有资源（含 ECS）均不使用；旧地址、映射和历史记录不代表当前可用来源",
                   "ces": "当前 HCS 8.5.1 局点无 CES；AUTO 不探测、不请求 CES。仅旧部署显式启用 "
                          "INSPECTOR_HCS_CES_ENABLED 并选择 CES 时保留兼容路径；DCS 不走 CES，实时内存使用原生数据",
                   "prometheus": "可选：填写租户已有 Prometheus（node_exporter）地址后可采集 ECS 使用率；"
                                 "AUTO 可选择已配置的此来源，否则仅保留服务原生字段；不使用 AOM"}})


# ---------------- 巡检任务 ----------------
class TaskPatch(BaseModel):
    p90_min_samples: int | None = Field(None, strict=True, ge=1, le=10000)
    interval_sec: int | None = Field(None, ge=30, le=7 * 86400)
    cron: str | None = None                  # 空字符串 = 改回按周期执行
    enabled: bool | None = None
    aom: None = Field(None, description="AOM 已全局停用；仅接受 null 清除旧映射，未传保留历史配置")

    @field_validator("aom", mode="before")
    @classmethod
    def _aom_disabled(cls, value: object) -> None:
        if value is not None:
            raise ValueError("AOM 已全局停用，所有任务均不支持 AOM 映射；仅可传 null 清除旧映射")
        return None


def _next_runs(db: Session) -> dict[str, str | None]:
    """调度进程心跳里上报的下次执行时间；调度进程没有运行或心跳超时时返回空（改用估算值）。"""
    hb = db.get(WorkerHeartbeat, "scheduler")
    timeout = settings_service.get_platform_alarm(db).heartbeat_timeout_min
    if hb is None or utcnow() - hb.beat_at > timedelta(minutes=timeout):
        return {}
    return dict((hb.info or {}).get("jobs") or {})


def _estimate_next(t: InspectionTask, last: datetime | None, now: datetime) -> datetime | None:
    """调度进程未上报时按周期 / crontab 估算下次执行时间（仅供参考）。"""
    if not t.enabled:
        return None
    if t.cron:
        try:
            return CronTrigger.from_crontab(t.cron, timezone=ZoneInfo(get_settings().timezone)).get_next_fire_time(None, now)
        except ValueError:
            return None
    nxt = (last or now) + timedelta(seconds=t.interval_sec)
    return max(nxt, now)


def _tenant_name(tm: dict, tid: int | None) -> str | None:
    return (tm[tid].short_name or tm[tid].name) if tid in tm else None


def _skipped(db: Session, code: str, scope: Scope, tm: dict) -> list[str]:
    if code in (CLEANUP_TASK,):
        return []
    return [t.label for t in pipeline.skipped_targets(db, code, scope.tenant_ids)]


@router.get("/tasks", summary="巡检任务与项目运行（每个租户项目独立保留最近记录）")
def tasks(db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
          _: object = Depends(require_perm("task:view")), project_ref: Annotated[int | None, Query(ge=1)] = None,
          unknown_project: bool = False):
    if project_ref is not None and unknown_project:
        raise BizError(ErrorCode.BAD_REQUEST, "不能同时选择已知项目和历史未知项目")
    tm = tenant_map(db)
    nxt = _next_runs(db)
    now = utcnow()
    all_scopes = platform_watch.task_scopes(db, tenant_ids=scope.tenant_ids, include_global=scope.full,
                                           project_ref=project_ref, unknown_project=unknown_project)
    out = []
    for t in db.scalars(select(InspectionTask).order_by(InspectionTask.code)):
        q = scope.apply(select(InspectionRun).where(InspectionRun.task_code == t.code), InspectionRun.tenant_id,
                        include_global=True)
        if project_ref is not None:
            q = q.where(InspectionRun.project_ref == project_ref)
        if unknown_project:
            q = q.where(InspectionRun.project_ref.is_(None),
                        or_(InspectionRun.task_code != CLEANUP_TASK, InspectionRun.tenant_id.is_not(None)))
        ranked = q.with_only_columns(InspectionRun.id, func.row_number().over(
            partition_by=(InspectionRun.tenant_id, InspectionRun.project_ref),
            order_by=(func.coalesce(InspectionRun.finished_at, InspectionRun.started_at).desc(),
                      InspectionRun.id.desc())).label("position")).subquery()
        runs = db.scalars(select(InspectionRun).join(ranked, InspectionRun.id == ranked.c.id)
                          .where(ranked.c.position <= 12)
                          .order_by(func.coalesce(InspectionRun.finished_at, InspectionRun.started_at).desc(),
                                    InspectionRun.id.desc())).all()
        current = [s for s in all_scopes if s["task_code"] == t.code]
        next_run = nxt.get(t.code) if nxt else _estimate_next(t, runs[0].started_at if runs else None, now)
        out.append({"code": t.code, "name": t.name, "service": t.service, "interval_sec": t.interval_sec,
                    "cron": t.cron, "enabled": t.enabled, "params": t.params, "next_run": next_run,
                    "next_run_source": "scheduler" if nxt else "estimate",
                    "types": collect_switch.TASK_TYPES.get(t.code, []), "note": TASK_NOTES.get(t.code),
                    "checks": list(catalog.TASK_BY_CODE[t.code].checks) if t.code in catalog.TASK_BY_CODE else [],
                    "steps": list(catalog.TASK_STEPS.get(t.code, ())),
                    "min_interval": MIN_INTERVAL.get(t.code, 30), "skipped_tenants": _skipped(db, t.code, scope, tm),
                    "scopes": [TaskScopeOut(**{k: v for k, v in s.items() if k != "run"},
                                           tenant_name=_tenant_name(tm, s["tenant_id"])).model_dump() for s in current],
                    "history_limit_per_scope": 12,
                    "runs": [TaskRunOut.model_validate(r).model_copy(update={
                        "tenant_name": _tenant_name(tm, r.tenant_id),
                        "project_scope": platform_watch.scope_kind(r.task_code, r.tenant_id, r.project_ref),
                        "project_label": f"项目#{r.project_ref}" if r.project_ref is not None else
                                         "全局内部任务" if r.task_code == CLEANUP_TASK and r.tenant_id is None else
                                         "历史未知项目（未记录）"}).model_dump() for r in runs]})
    return ok(out)


@router.put("/tasks/{code}", summary="修改任务：周期（秒）/ crontab / 启停 / 清除旧 AOM 映射（调度进程最迟 30 秒生效）")
def update_task(code: str, body: TaskPatch, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                _: CurrentUser = Depends(require_perm("task:edit")), scope: Scope = Depends(get_scope)):
    if not scope.full:
        raise BizError(ErrorCode.FORBIDDEN, "任务调度为全局配置，修改需要全部租户的数据权限", 403)
    t = db.get(InspectionTask, code)
    if t is None:
        raise not_found("巡检任务")
    data = body.model_dump(exclude_unset=True)
    before: dict[str, object] = {"interval_sec": t.interval_sec, "cron": t.cron, "enabled": t.enabled}
    if "p90_min_samples" in data:
        if code != "dgc_batch_instance":
            raise BizError(ErrorCode.BAD_REQUEST, "P90 样本数仅用于 DataArts 批作业任务")
        params = dict(t.params or {})
        before["p90_min_samples"] = params.get("p90_min_samples", 5)
        if data["p90_min_samples"] is None:
            params.pop("p90_min_samples", None)
        else:
            params["p90_min_samples"] = data["p90_min_samples"]
        t.params = params
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
    if "aom" in data:
        params = dict(t.params or {})
        previous = params.get("aom")
        before["aom"] = ({key: value for key, value in previous.items() if key in AOM_MAPPING_FIELDS}
                         if isinstance(previous, dict) else "旧格式映射" if previous is not None else None)
        params.pop("aom", None)
        t.params = params
    after: dict[str, object] = {"interval_sec": t.interval_sec, "cron": t.cron, "enabled": t.enabled}
    if "p90_min_samples" in data:
        after["p90_min_samples"] = (t.params or {}).get("p90_min_samples", 5)
    if "aom" in data:
        after["aom"] = (t.params or {}).get("aom")
    diff = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    if "enabled" in diff and code in ("gaussdb_status", "gaussdb_storage", "gaussdb_components"):
        db.flush()
        inspection_evidence.sync_gauss_task_requirements(db)
    if diff:
        audit.log("task.update", target_type="task", target_id=code, target_name=t.name, detail={"changes": diff})
    else:
        db.commit()
    return ok({**after, "code": code, "params": t.params or {}, "changed": bool(diff)})


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
    items = [{"run_id": r.id, "tenant_id": r.tenant_id, "project_ref": r.project_ref,
              "tenant_name": _tenant_name(tm, r.tenant_id),
              "status": r.status, "resource_total": r.resource_total, "abnormal_total": r.abnormal_total,
              "api_calls": r.api_calls, "error_summary": r.error_summary, "stats": r.stats or {}} for r in runs]
    failed = [i for i in items if i["status"] in ("FAILED", "TIMEOUT")]
    outcome = platform_watch.run_outcome(runs, skipped=bool(skipped))
    audit.log("task.run", target_type="task", target_id=code, target_name=task.name,
              tenant_id=scope.single, result="SUCCESS" if outcome == "SUCCESS" else "FAILED",
              detail={"outcome": outcome, "runs": [{"tenant": i["tenant_name"], "project_ref": i["project_ref"],
                      "status": i["status"]} for i in items], "skipped": skipped})
    if outcome == "FAILED":
        raise BizError(ErrorCode.HCS_UPSTREAM, "执行失败：" + "；".join(
            f"{i['tenant_name'] or '默认'}：{i['error_summary']}" for i in failed)[:500], 502, {"runs": items})
    return ok({"runs": items, "skipped": skipped, "status": outcome})


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


# ---------------- 采集范围 ----------------
def _check_scope_target(db: Session, scope: Scope, tenant_id: int, resource_type: str) -> None:
    """采集范围的授权口径与采集开关一致：全局（0）需要全部租户的数据权限；租户设置需要在数据范围内。"""
    if resource_type not in collect_switch.SWITCH_TYPES:
        raise BizError(ErrorCode.BAD_REQUEST, f"未知资源类型：{resource_type}")
    if tenant_id == collect_scope.GLOBAL:
        if not scope.full:
            raise BizError(ErrorCode.FORBIDDEN, "修改全局采集范围需要全部租户的数据权限，请在租户下单独设置", 403)
        return
    if tenant_id not in tenant_map(db):
        raise BizError(ErrorCode.BAD_REQUEST, f"租户不存在：{tenant_id}")
    if not scope.allows(tenant_id) and not scope.full:
        raise BizError(ErrorCode.FORBIDDEN, "无权修改其他租户的采集范围", 403)


def _scope_text(rules: list[dict] | None, tenant_id: int) -> list[str]:
    if rules is None:
        return ["跟随全局"] if tenant_id else ["全部采集"]
    return collect_scope.describe_rules(rules) or ["全部采集"]


@router.get("/collect-scopes", summary="采集范围：每类资源的全局范围、各租户的设置与生效规则、资源数与范围外资源数")
def get_scopes(db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
               _: object = Depends(require_perm("task:view", "collect:manage", any_of=True))):
    return ok({**collect_scope.overview(db, scope.tenant_ids), "global_editable": scope.full})


@router.get("/collect-scopes/{tenant_id}/{resource_type}/candidates",
            summary="采集范围的候选值：该租户（0 = 全部租户）已知资源在某个维度上的取值与数量")
def scope_candidates(tenant_id: int, resource_type: str, dim: str = Query(..., max_length=32),
                     keyword: str | None = Query(None, max_length=100), limit: int = Query(50, ge=1, le=200),
                     db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                     _: CurrentUser = Depends(require_perm("collect:manage"))):
    _check_scope_target(db, scope, tenant_id, resource_type)
    return ok(collect_scope.candidates(db, tenant_id, resource_type, dim, keyword, limit,
                                       visible_tenants=scope.tenant_ids))


@router.post("/collect-scopes/{tenant_id}/{resource_type}/preview",
             summary="保存前预估：这组规则会让多少资源落到范围外、其中新排除多少、将关闭多少活动告警")
def scope_preview(tenant_id: int, resource_type: str, body: ScopeIn, db: Session = Depends(get_db),
                  scope: Scope = Depends(get_scope), _: CurrentUser = Depends(require_perm("collect:manage"))):
    _check_scope_target(db, scope, tenant_id, resource_type)
    rules = None if body.rules is None else [r.model_dump(exclude_none=True) for r in body.rules]
    return ok(collect_scope.preview(db, tenant_id, resource_type, rules, visible_tenants=scope.tenant_ids))


@router.put("/collect-scopes/{tenant_id}/{resource_type}",
            summary="保存采集范围（范围外的资源不再采集 / 评估 / 告警，其活动告警自动关闭；台账仍完整记录）")
def save_scope(tenant_id: int, resource_type: str, body: ScopeIn, db: Session = Depends(get_db),
               scope: Scope = Depends(get_scope), audit: Auditor = Depends(get_auditor),
               cu: CurrentUser = Depends(require_perm("collect:manage"))):
    _check_scope_target(db, scope, tenant_id, resource_type)
    rules = None if body.rules is None else [r.model_dump(exclude_none=True) for r in body.rules]
    res = collect_scope.save(db, tenant_id, resource_type, rules, cu.display_name or cu.username)
    if res["changed"]:
        label = _tenant_name(tenant_map(db), tenant_id) or "全局"
        audit.log("collect.scope", target_type="collect_scope", target_id=f"{tenant_id}:{resource_type}",
                  target_name=f"{label} · {catalog.type_name(resource_type)}",
                  tenant_id=tenant_id or scope.single,
                  detail={"before": _scope_text(res["before"], tenant_id), "after": _scope_text(res["after"], tenant_id),
                          "excluded": res["excluded"], "restored": res["restored"],
                          "closed_alarms": res["closed_alarms"]})
    else:
        db.commit()
    return ok(res)


# ---------------- DataArts 精确监控作业（复用采集范围权限与物化） ----------------
def _check_job_selection_target(scope: Scope, tenant_id: int) -> None:
    if tenant_id < 1 or not scope.allows(tenant_id):
        raise BizError(ErrorCode.FORBIDDEN, "无权访问该租户的作业监控范围", 403)


@router.get("/job-selections/targets", summary="作业监控范围：获授权的租户、项目与已配置工作空间")
def job_selection_targets(db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                          _: object = Depends(require_perm("task:view", "collect:manage", any_of=True))):
    return ok(job_selection.targets(db, scope))


@router.get("/job-selections/{tenant_id}/{resource_type}", summary="读取项目/工作空间的精确监控名单")
def get_job_selection(tenant_id: int, resource_type: JobResourceType, project_ref: int = Query(..., ge=1),
                      workspace: str = Query(..., max_length=128), db: Session = Depends(get_db),
                      scope: Scope = Depends(get_scope),
                      _: object = Depends(require_perm("task:view", "collect:manage", any_of=True))):
    _check_job_selection_target(scope, tenant_id)
    return ok(job_selection.read(db, tenant_id, resource_type, project_ref, workspace))


@router.get("/job-selections/{tenant_id}/{resource_type}/jobs", summary="分页发现 DataArts 作业定义（仅 GET，不查状态）")
def discover_selection_jobs(tenant_id: int, resource_type: JobResourceType, project_ref: int = Query(..., ge=1),
                            workspace: str = Query(..., max_length=128), page: int = Query(1, ge=1),
                            page_size: int = Query(100, ge=1, le=200), keyword: str = Query("", max_length=128),
                            db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                            _: CurrentUser = Depends(require_perm("collect:manage"))):
    _check_job_selection_target(scope, tenant_id)
    return ok(job_selection.discover(db, tenant_id, resource_type, project_ref, workspace,
                                    page=page, page_size=page_size, keyword=keyword))


@router.put("/job-selections/{tenant_id}/{resource_type}", summary="保存已批准的作业范围（不改变采集开关）")
def save_job_selection(tenant_id: int, resource_type: JobResourceType, body: JobSelectionIn,
                       db: Session = Depends(get_db), scope: Scope = Depends(get_scope),
                       audit: Auditor = Depends(get_auditor), cu: CurrentUser = Depends(require_perm("collect:manage"))):
    _check_job_selection_target(scope, tenant_id)
    result = job_selection.save(db, tenant_id, resource_type, body, cu.display_name or cu.username)
    if result["changed"]:
        audit.log("collect.job_selection", target_type="job_selection",
                  target_id=f"{tenant_id}:{body.project_ref}:{resource_type}", tenant_id=tenant_id,
                  detail={"project_ref": body.project_ref, "workspace": body.workspace,
                          "before": result["before"], "after": result["after"],
                          "closed_alarms": result["closed_alarms"]})
    else:
        db.commit()
    return ok(result)


# ---------------- 系统设置 ----------------
def _can(cu: CurrentUser, perm: str) -> bool:
    return cu.can(perm)


def _fields(key: str) -> list[dict]:
    model = settings_service.MODELS[key]
    out = []
    for name, f in model.model_fields.items():
        item: dict = {"key": name, "label": f.description or name,
                      "default": f.get_default(call_default_factory=True)}
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
             _: object = Depends(require_perm("task:view")), project_ref: Annotated[int | None, Query(ge=1)] = None,
             unknown_project: bool = False):
    if project_ref is not None and unknown_project:
        raise BizError(ErrorCode.BAD_REQUEST, "不能同时选择已知项目和历史未知项目")
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
    all_scopes = platform_watch.task_scopes(db, tenant_ids=scope.tenant_ids, include_global=scope.full,
                                           project_ref=project_ref, unknown_project=unknown_project)
    for t in db.scalars(select(InspectionTask).order_by(InspectionTask.code)):
        selected = [s for s in all_scopes if s["task_code"] == t.code]
        tenants = [TaskScopeOut(**{k: v for k, v in p.items() if k != "run"},
                               tenant_name=_tenant_name(tm, p["tenant_id"])).model_dump() for p in selected]
        health.append({"code": t.code, "name": t.name, "enabled": t.enabled, "interval_sec": t.interval_sec,
                       "cron": t.cron, "tenants": tenants,
                       "status": platform_watch.task_health(selected, cfg.task_fail_threshold, enabled=t.enabled)})
    alarms_q = scope.apply(select(AlarmEvent).where(AlarmEvent.service == platform_watch.PLATFORM,
                                                    AlarmEvent.status.in_(ACTIVE_STATUSES)),
                           AlarmEvent.tenant_id, include_global=True)
    if project_ref is not None:
        alarms_q = alarms_q.where(AlarmEvent.detail["project_ref"].as_integer() == project_ref)
    if unknown_project:
        alarms_q = alarms_q.where(AlarmEvent.detail["project_ref"].as_integer().is_(None))
    alarms = db.scalars(alarms_q.order_by(AlarmEvent.first_seen.desc()).limit(20)).all()
    cleanup = db.scalar(select(InspectionRun).where(InspectionRun.task_code == CLEANUP_TASK)
                        .order_by(InspectionRun.started_at.desc()).limit(1))
    silences = len(silence_service.active_silences(db, now))
    redis_ok = None
    if s.redis_url:
        redis_ok = _redis_health(s.redis_url)
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
