"""平台自身告警（服务 = platform）：平台自己出问题时也要有人知道。

| 规则 | 触发 | 恢复 |
| R-PLAT-001 巡检任务连续失败 | FAILED/TIMEOUT/PARTIAL/INTERRUPTED 累计；排队/取消/跳过不清零 | 同范围真实 SUCCESS |
| R-PLAT-002 租户认证失败 | 任务报 401 / 403（账号密码错误、AK/SK 失效、权限被回收） | 同任务/租户/项目真实 SUCCESS |
| R-PLAT-003 通知发送失败 | 通知重试 5 次后仍失败（SMTP 不可用、收件人被拒） | 之后有通知发送成功 |
| R-PLAT-004 后台进程心跳超时 | 调度 / 通知进程超过 N 分钟没有心跳（默认 3） | 心跳恢复 |

平台告警与业务告警走同一套告警中心、通知路由和静默；没有通知路由命中时，发给“系统设置 → 平台自身告警”里的收件人，
仍为空时发给有邮箱的平台管理员。
"""
import hashlib
import logging
import os
import re
import socket
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, event, func, or_, select
from sqlalchemy.orm import Session

from app import __version__
from app.core.config import get_settings
from app.models import (
    AlarmEvent,
    AlarmEventLog,
    HcsAccount,
    InspectionRun,
    InspectionTask,
    Notification,
    Role,
    TaskExecution,
    User,
    WorkerHeartbeat,
)
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow

from . import settings_service
from .recipients import emails_for_users

log = logging.getLogger(__name__)
PLATFORM = "platform"
RULES: dict[str, dict[str, str]] = {
    "R-PLAT-001": {"name": "巡检任务连续失败", "resource_type": "platform_task", "check_item": "task_failures",
                   "runbook": "1. 系统管理 → 巡检任务，展开该任务查看各租户最近运行的错误摘要\n"
                              "2. 超时 / 5xx：在巡检服务器上执行 make probe TENANT=<租户代码> ONLY=<服务>，确认接口可达、耗时\n"
                              "3. 404 / 字段缺失：核对租户接入里的服务 Endpoint、项目 ID 是否正确（不同 Region 的地址不同）\n"
                              "4. 修复后点“立即执行”，执行成功即自动恢复；该服务确实不用时可在“采集开关”关闭"},
    "R-PLAT-002": {"name": "租户认证失败", "resource_type": "platform_tenant", "check_item": "tenant_auth",
                   "runbook": "1. 系统管理 → 租户接入，对该租户点“连通性测试”查看认证步骤的报错\n"
                              "2. 401：IAM 用户密码已修改 / 账号被锁定，或 AK/SK 已删除；更新凭据后保存\n"
                              "3. 403：账号缺少对应服务的只读权限（例如 MRS ReadOnlyAccess），请联系租户管理员授权\n"
                              "4. 同任务、同租户、同项目真实执行成功后恢复；历史未知项目不可推断恢复"},
    "R-PLAT-003": {"name": "通知发送失败", "resource_type": "platform_notify", "check_item": "notify_failed",
                   "runbook": "1. 通知管理 → SMTP 与测试邮件，向自己发一封测试邮件，查看报错\n"
                              "2. Connection refused / timeout：检查 SMTP 地址、端口、防火墙；STARTTLS 与端口是否匹配\n"
                              "3. 535 / Authentication：检查 .env 中的 SMTP 账号密码，修改后执行 make restart\n"
                              "4. 550 / 收件人被拒：检查联系人邮箱、邮件网关白名单；之后有通知发送成功即自动恢复"},
    "R-PLAT-004": {"name": "后台进程心跳超时", "resource_type": "platform_worker", "check_item": "heartbeat",
                   "runbook": "1. 在部署目录执行 make status 查看 scheduler / notifier 容器是否在运行、是否反复重启\n"
                              "2. make logs S=scheduler（或 notifier）查看最近的报错（常见：数据库连接失败、磁盘满）\n"
                              "3. 执行 docker-compose up -d scheduler notifier 重新拉起；心跳恢复后自动恢复\n"
                              "4. 调度进程停止期间巡检不会执行，恢复后检查“巡检任务”页各任务的最近运行"},
}
AUTH_ERROR = re.compile(r"\[(401|403)\]")
WORKERS = {"scheduler": "调度进程", "notifier": "通知进程"}
UNSUCCESSFUL = ("FAILED", "TIMEOUT", "PARTIAL", "INTERRUPTED")
NEUTRAL = ("QUEUED", "RUNNING", "SKIPPED", "CANCELLED")
_PENDING_RUNS = "_platform_watch_pending_runs"
_COMMITTED_RUNS = "_platform_watch_committed_runs"


def defer_on_run(db: Session, run_id: int) -> None:
    """Queue one outcome after its enclosing transaction commits.

    Durable finish owns the ACTIVE-to-terminal guard and calls this before
    committing. A fresh Session is required because after_commit cannot issue
    SQL on the Session whose transaction just completed. Dispatch waits until
    transaction end so its original connection is released even with pool_size=1.
    """
    transaction = db.get_nested_transaction() or db.get_transaction()
    if transaction is None:
        raise RuntimeError("任务终态必须在事务内登记平台告警")
    pending = db.info.setdefault(_PENDING_RUNS, {})
    pending.setdefault(run_id, transaction)


@event.listens_for(Session, "after_commit")
def _mark_committed_runs(db: Session) -> None:
    if db.in_nested_transaction():
        return
    db.info[_COMMITTED_RUNS] = db.info.pop(_PENDING_RUNS, {})


@event.listens_for(Session, "after_transaction_end")
def _publish_committed_runs(db: Session, transaction) -> None:
    if transaction.parent is not None:
        return
    pending = db.info.pop(_COMMITTED_RUNS, {})
    for run_id in pending:
        try:
            with Session(bind=db.get_bind(), expire_on_commit=False, autoflush=False) as watched:
                run = watched.get(InspectionRun, run_id)
                if run is not None and run.finished_at is not None and run.status not in ("QUEUED", "RUNNING"):
                    _on_run(watched, run)
                    watched.commit()
        except Exception:
            log.exception("platform watch failed after committing run %s", run_id)


@event.listens_for(Session, "after_soft_rollback")
def _discard_rolled_back_runs(db: Session, previous_transaction) -> None:
    def belongs(transaction) -> bool:
        while transaction is not None:
            if transaction is previous_transaction:
                return True
            transaction = transaction.parent
        return False

    if previous_transaction.parent is None:
        db.info.pop(_PENDING_RUNS, None)
    else:
        db.info[_PENDING_RUNS] = {run_id: transaction for run_id, transaction in db.info.get(_PENDING_RUNS, {}).items()
                                if not belongs(transaction)}


def run_outcome(runs: Sequence, *, skipped: bool = False) -> str:
    """Shared manual/scheduler conclusion; only every requested execution succeeding is SUCCESS."""
    statuses = [r.status for r in runs]
    if not statuses or all(status == "SKIPPED" for status in statuses):
        return "SKIPPED"
    if any(status == "RUNNING" for status in statuses):
        return "RUNNING"
    if any(status == "QUEUED" for status in statuses):
        return "QUEUED"
    if all(status == "CANCELLED" for status in statuses):
        return "CANCELLED"
    if all(status in ("FAILED", "TIMEOUT", "INTERRUPTED") for status in statuses):
        return "FAILED"
    if all(status == "SUCCESS" for status in statuses) and not skipped:
        return "SUCCESS"
    return "PARTIAL"


def scope_kind(task_code: str, tenant_id: int | None, project_ref: int | None) -> str:
    if project_ref is not None:
        return "PROJECT"
    return "GLOBAL" if task_code == "data_cleanup" and tenant_id is None else "UNKNOWN"


def task_scopes(db: Session, *, tenant_ids: list[int] | None = None, include_global: bool = False,
                task_code: str | None = None, project_ref: int | None = None,
                unknown_project: bool = False) -> list[dict[str, Any]]:
    """One deterministic row per task/tenant/project, without a global history limit.

    SQL windows count unsuccessful completed attempts back to the last real
    SUCCESS. QUEUED/RUNNING/SKIPPED/CANCELLED neither reset nor increment.
    Ordering uses completion time, with ID as a stable tie breaker.
    """
    model = InspectionRun
    group = (model.task_code, model.tenant_id, model.project_ref)
    order = (func.coalesce(model.finished_at, model.started_at).desc(), model.id.desc())
    known = or_(model.project_ref.is_not(None), and_(model.task_code == "data_cleanup", model.tenant_id.is_(None)))
    success = and_(model.status == "SUCCESS", model.finished_at.is_not(None), known)
    q = select(model.id, model.task_code, model.tenant_id, model.project_ref, model.status,
               model.started_at, model.finished_at, model.resource_total, model.abnormal_total,
               func.row_number().over(partition_by=group, order_by=order).label("position"),
               func.sum(case((success, 1), else_=0)).over(partition_by=group, order_by=order,
                                                        rows=(None, 0)).label("successes_seen"),
               func.sum(case((model.status.not_in(NEUTRAL), 1), else_=0)).over(
                   partition_by=group, order_by=order, rows=(None, 0)).label("attempts_seen"))
    if tenant_ids is not None:
        condition = model.tenant_id.in_(tenant_ids)
        q = q.where(or_(condition, model.tenant_id.is_(None)) if include_global else condition)
    if task_code is not None:
        q = q.where(model.task_code == task_code)
    if project_ref is not None:
        q = q.where(model.project_ref == project_ref)
    if unknown_project:
        q = q.where(model.project_ref.is_(None),
                    or_(model.task_code != "data_cleanup", model.tenant_id.is_not(None)))
    ranked = q.subquery()
    c = ranked.c
    stats = select(c.task_code, c.tenant_id, c.project_ref,
        func.max(case((c.position == 1, c.id))).label("latest_id"),
        func.sum(case((and_(c.status.in_(UNSUCCESSFUL), c.successes_seen == 0), 1), else_=0)).label("fails"),
        func.max(case((and_(c.status == "SUCCESS", c.finished_at.is_not(None)), c.finished_at))).label("last_success_at"),
        func.max(case((and_(c.status.not_in(NEUTRAL), c.attempts_seen == 1), c.status))).label("effective_status"),
        func.max(case((and_(c.status.not_in(NEUTRAL), c.attempts_seen == 1), c.id))).label("effective_run_id"),
        func.max(case((and_(c.status.not_in(NEUTRAL), c.attempts_seen == 1), c.finished_at))).label("effective_finished_at"),
        func.max(case((and_(c.status.not_in(NEUTRAL), c.attempts_seen == 1), c.resource_total))).label(
            "effective_resource_total"),
        func.max(case((and_(c.status.not_in(NEUTRAL), c.attempts_seen == 1), c.abnormal_total))).label(
            "effective_abnormal_total")
    ).group_by(c.task_code, c.tenant_id, c.project_ref).subquery()
    rows = db.execute(select(model, stats.c.fails, stats.c.last_success_at, stats.c.effective_status,
                             stats.c.effective_run_id, stats.c.effective_finished_at,
                             stats.c.effective_resource_total, stats.c.effective_abnormal_total)
                      .join(stats, model.id == stats.c.latest_id)
                      .order_by(model.task_code, model.tenant_id, model.project_ref)).all()
    return [{"run": run, "task_code": run.task_code, "tenant_id": run.tenant_id, "project_ref": run.project_ref,
             "project_scope": scope_kind(run.task_code, run.tenant_id, run.project_ref),
             "project_label": f"项目#{run.project_ref}" if run.project_ref is not None else
                              "全局内部任务" if scope_kind(run.task_code, run.tenant_id, run.project_ref) == "GLOBAL"
                              else "历史未知项目（未记录）",
             "last_status": run.status, "last_at": run.finished_at or run.started_at,
             "resource_total": run.resource_total, "abnormal_total": run.abnormal_total,
             "last_success_at": ok_at, "fails": int(fails or 0), "effective_status": effective_status,
             "effective_run_id": effective_id, "effective_finished_at": effective_finished,
             "effective_resource_total": effective_resources, "effective_abnormal_total": effective_abnormal}
            for run, fails, ok_at, effective_status, effective_id, effective_finished,
            effective_resources, effective_abnormal in rows]


def task_health(scopes: list[dict], threshold: int, *, enabled: bool = True) -> str:
    if not enabled:
        return "DISABLED"
    if not scopes:
        return "UNKNOWN"
    if any(s["fails"] >= threshold for s in scopes):
        return "FAILED"
    if any(s["fails"] or s["effective_status"] != "SUCCESS" or s["project_scope"] == "UNKNOWN"
           or s["last_success_at"] is None or s["last_status"] in NEUTRAL for s in scopes):
        return "WARN"
    return "OK"


def scope_execution_status(scope: dict) -> str:
    status = scope["last_status"]
    if status == "SUCCESS" and (scope["run"].finished_at is None or
                                scope["project_scope"] == "UNKNOWN" and scope["fails"]):
        return "PARTIAL"
    if status in NEUTRAL and scope["effective_status"] in UNSUCCESSFUL:
        return scope["effective_status"]
    return status if status in (*UNSUCCESSFUL, *NEUTRAL, "SUCCESS") else "PARTIAL"


def task_last_status(scopes: list[dict]) -> str | None:
    values = [scope_execution_status(scope) for scope in scopes]
    return next((status for status in ("FAILED", "TIMEOUT", "INTERRUPTED", "PARTIAL", "RUNNING", "QUEUED",
                                       "CANCELLED", "SKIPPED", "SUCCESS")
                 if status in values), None)


def _fp(rule_id: str, key: str) -> str:
    return hashlib.sha1(f"platform|{rule_id}|{key}".encode()).hexdigest()


def _active(db: Session, rule_id: str, key: str) -> AlarmEvent | None:
    return db.scalar(select(AlarmEvent).where(AlarmEvent.fingerprint == _fp(rule_id, key),
                                              AlarmEvent.status.in_(ACTIVE_STATUSES)))


def fallback_recipients(db: Session) -> list[str]:
    cfg = settings_service.get_platform_alarm(db)
    if cfg.recipient_user_ids:
        # 已配置人员全部失效时不扩大投递范围，不偷偷改发管理员。
        return emails_for_users(db, cfg.recipient_user_ids)
    admin = db.scalar(select(Role.id).where(Role.code == "admin"))
    if admin is None:
        return []
    ids = list(db.scalars(select(User.id).where(User.role_id == admin).order_by(User.id)))
    return emails_for_users(db, ids)


def raise_alarm(db: Session, rule_id: str, key: str, *, name: str, title: str, tenant_id: int | None = None,
                detail: dict | None = None, now: datetime | None = None) -> AlarmEvent | None:
    """产生（或刷新）一条平台告警；已存在活动告警时只刷新标题和次数。返回新产生的告警。"""
    from . import alarm_service, silence_service  # 延迟导入：alarm_service → platform_watch 不形成循环

    cfg = settings_service.get_platform_alarm(db)
    if not cfg.enabled:
        return None
    now = now or utcnow()
    active = _active(db, rule_id, key)
    if active is not None:
        active.last_seen, active.count, active.title = now, active.count + 1, title
        active.detail = {**(active.detail or {}), **(detail or {})}
        return None
    rule = RULES[rule_id]
    alarm = AlarmEvent(fingerprint=_fp(rule_id, key), rule_id=rule_id, resource_ref=None, tenant_id=tenant_id,
                       service=PLATFORM, resource_type=rule["resource_type"], resource_name=name[:255], sub_object="",
                       severity=cfg.severity, status="FIRING", title=title[:255], check_item=rule["check_item"],
                       current_value=None, threshold_text=None, owner=None, first_seen=now, last_seen=now,
                       detail={"platform": True, "key": key,
                               **({"tenant": _tenant_label(db, tenant_id)} if tenant_id is not None else {}),
                               **(detail or {})})
    db.add(alarm)
    db.flush()
    db.add(AlarmEventLog(alarm_id=alarm.id, action="FIRE", to_status="FIRING", operator="system",
                         note=f"{rule['name']}：{title}", at=now))
    silence = silence_service.find_silence(db, alarm, now)
    if silence is not None:
        silence_service.suppress(db, alarm, silence, now)
    elif alarm_service.enqueue_notification(db, alarm, "FIRING", now) is None:
        to = fallback_recipients(db)
        if to:
            alarm_service.enqueue_notification(db, alarm, "FIRING", now, recipients=to)
    log.warning("platform alarm %s: %s", rule_id, title)
    return alarm


def resolve_alarm(db: Session, rule_id: str, key: str, note: str, now: datetime | None = None) -> bool:
    from . import alarm_service

    active = _active(db, rule_id, key)
    if active is None:
        return False
    now = now or utcnow()
    prev = active.status
    active.status, active.resolved_at, active.last_seen = "RESOLVED", now, now
    db.add(AlarmEventLog(alarm_id=active.id, action="RESOLVE", from_status=prev, to_status="RESOLVED",
                         operator="system", note=note, at=now))
    if prev != "SUPPRESSED" and alarm_service.enqueue_notification(db, active, "RESOLVED", now) is None:
        to = fallback_recipients(db)
        if to:
            alarm_service.enqueue_notification(db, active, "RESOLVED", now, recipients=to)
    return True


def _local(t: datetime) -> str:
    return t.astimezone(ZoneInfo(get_settings().timezone)).strftime("%m-%d %H:%M:%S")


def _tenant_label(db: Session, tenant_id: int | None) -> str:
    if tenant_id is None:
        return "平台"
    t = db.get(HcsAccount, tenant_id)
    return (t.short_name or t.name) if t else f"租户#{tenant_id}"


def on_run(db: Session, run: InspectionRun, *, commit: bool = True) -> None:
    """每次任务运行结束后调用（巡检 / 台账同步 / 数据清理）。任何异常只记日志，不影响任务本身。"""
    if not commit:
        # Durable execution owns the terminal transition and its transaction.
        # Isolate alert failures in a savepoint without discarding that result.
        try:
            db.flush()
            with db.begin_nested():
                _on_run(db, run)
                db.flush()
        except Exception:  # noqa: BLE001
            log.exception("platform watch failed inside the run transaction %s", run.id)
        return
    # Durable lifecycle registered the terminal outcome before commit. The old
    # pipeline/ledger callback must not emit the same platform outcome again.
    try:
        if db.scalar(select(TaskExecution.id).where(TaskExecution.run_id == run.id).limit(1)) is not None:
            return
        _on_run(db, run)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("platform watch failed for run %s", run.id)


def _on_run(db: Session, run: InspectionRun) -> None:
    cfg = settings_service.get_platform_alarm(db)
    if not cfg.enabled or run.status in NEUTRAL:
        return
    scopes = task_scopes(db, task_code=run.task_code)
    current = next((s for s in scopes if s["tenant_id"] == run.tenant_id and s["project_ref"] == run.project_ref), None)
    if current is None or current["effective_run_id"] != run.id:
        return  # delayed callbacks cannot overwrite evidence from a later completed execution
    key = f"{run.task_code}:{run.tenant_id or 0}" + (f":project:{run.project_ref}" if run.project_ref is not None else "")
    auth_key = (f"tenant:{run.tenant_id}:project:{run.project_ref}:task:{run.task_code}"
                if run.project_ref is not None else f"tenant:{run.tenant_id}")
    task = db.get(InspectionTask, run.task_code)
    task_name = task.name if task else run.task_code
    tenant = _tenant_label(db, run.tenant_id)
    label = current["project_label"]
    if run.status == "SUCCESS" and run.finished_at is not None:
        if current["project_scope"] == "UNKNOWN":
            return  # NULL does not prove that two historical runs refer to the same real project
        resolve_alarm(db, "R-PLAT-001", key, f"{task_name}（{tenant} / {label}）执行成功，自动恢复")
        if run.tenant_id is not None:
            active = _active(db, "R-PLAT-002", auth_key)
            failed_at = (active.detail or {}).get("last_failure_at") if active is not None else None
            if isinstance(failed_at, str) and run.finished_at >= datetime.fromisoformat(failed_at):
                resolve_alarm(db, "R-PLAT-002", auth_key, f"{tenant} / {label} 的任务执行成功，认证已恢复")
        return
    if run.status not in UNSUCCESSFUL:
        return
    error = (run.error_summary or "")[:300]
    detail = {"task": run.task_code, "project_ref": run.project_ref, "project_scope": current["project_scope"],
              "project_label": label, "error": error, "last_failure_run_id": run.id,
              "last_failure_at": (run.finished_at or run.started_at).isoformat()}
    if cfg.auth_fail_alarm and run.tenant_id is not None and AUTH_ERROR.search(error):
        active = _active(db, "R-PLAT-002", auth_key)
        previous = (active.detail or {}).get("last_failure_at") if active is not None else None
        if previous is None or datetime.fromisoformat(previous) <= (run.finished_at or run.started_at):
            raise_alarm(db, "R-PLAT-002", auth_key, name="HCS 接口认证",
                        title=f"认证失败（{task_name} / {label}）：{error[:60]}", tenant_id=run.tenant_id, detail=detail)
    if current["fails"] >= cfg.task_fail_threshold:
        raise_alarm(db, "R-PLAT-001", key, name=task_name,
                    title=f"连续 {current['fails']} 次执行未成功（{label}）：{error[:60]}", tenant_id=run.tenant_id,
                    detail={**detail, "failures": current["fails"], "threshold": cfg.task_fail_threshold})


def on_notification_failed(db: Session, n: Notification) -> None:
    cfg = settings_service.get_platform_alarm(db)
    if not cfg.notify_fail_alarm:
        return
    alarm = db.get(AlarmEvent, n.alarm_id) if n.alarm_id else None
    if alarm is not None and alarm.service == PLATFORM and alarm.rule_id == "R-PLAT-003":
        return                                   # 通知失败告警本身发不出去时不再套娃
    raise_alarm(db, "R-PLAT-003", "smtp", name="告警通知（SMTP）",
                title=f"重试 {n.retry} 次后仍发送失败：{(n.error or '')[:60]}",
                detail={"notification_id": n.id, "error": (n.error or "")[:300], "recipients": n.recipients[:5]})


def on_notification_sent(db: Session) -> None:
    resolve_alarm(db, "R-PLAT-003", "smtp", "通知已恢复正常发送")


def beat(db: Session, name: str, info: dict | None = None) -> None:
    """写进程心跳（scheduler / notifier 每 30 秒一次）。"""
    hb = db.get(WorkerHeartbeat, name)
    now = utcnow()
    if hb is None:
        hb = WorkerHeartbeat(name=name, started_at=now)
        db.add(hb)
    hb.host, hb.pid, hb.version, hb.beat_at = socket.gethostname()[:128], os.getpid(), __version__, now
    if info is not None:
        hb.info = info
    db.commit()


def check_heartbeats(db: Session, workers: list[str] | None = None, now: datetime | None = None) -> list[str]:
    """检查进程心跳，超时的产生 R-PLAT-004；恢复的自动恢复。返回超时的进程名。由另一个进程调用（互相看护）。"""
    cfg = settings_service.get_platform_alarm(db)
    now = now or utcnow()
    late = []
    for name in workers or list(WORKERS):
        hb = db.get(WorkerHeartbeat, name)
        if hb is None:                         # 从未启动过（例如开发环境只跑 API）：不告警
            continue
        if now - hb.beat_at > timedelta(minutes=cfg.heartbeat_timeout_min):
            late.append(name)
            mins = int((now - hb.beat_at).total_seconds() // 60)
            raise_alarm(db, "R-PLAT-004", name, name=f"{WORKERS.get(name, name)}（{hb.host}）",
                        title=f"已 {mins} 分钟没有心跳（最后心跳 {_local(hb.beat_at)}）",
                        detail={"worker": name, "host": hb.host, "beat_at": hb.beat_at.isoformat()}, now=now)
        else:
            resolve_alarm(db, "R-PLAT-004", name, f"{WORKERS.get(name, name)} 心跳已恢复", now=now)
    db.commit()
    return late
