"""平台自身告警（服务 = platform）：平台自己出问题时也要有人知道。

| 规则 | 触发 | 恢复 |
| R-PLAT-001 巡检任务连续失败 | 同一任务、同一租户连续失败 N 次（默认 3，系统设置可改） | 该任务该租户下一次执行成功 |
| R-PLAT-002 租户认证失败 | 任务报 401 / 403（账号密码错误、AK/SK 失效、权限被回收） | 该租户任一任务执行成功 |
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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
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
    User,
    WorkerHeartbeat,
)
from app.models.alarm import ACTIVE_STATUSES
from app.models.base import utcnow

from . import settings_service

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
                              "4. 该租户任一任务执行成功后自动恢复"},
    "R-PLAT-003": {"name": "通知发送失败", "resource_type": "platform_notify", "check_item": "notify_failed",
                   "runbook": "1. 通知管理 → SMTP 与测试邮件，向自己发一封测试邮件，查看报错\n"
                              "2. Connection refused / timeout：检查 SMTP 地址、端口、防火墙；STARTTLS 与端口是否匹配\n"
                              "3. 535 / Authentication：检查 .env 中的 SMTP 账号密码，修改后执行 make restart\n"
                              "4. 550 / 收件人被拒：检查联系人邮箱、邮件网关白名单；之后有通知发送成功即自动恢复"},
    "R-PLAT-004": {"name": "后台进程心跳超时", "resource_type": "platform_worker", "check_item": "heartbeat",
                   "runbook": "1. 在部署目录执行 make status 查看 scheduler / notifier 容器是否在运行、是否反复重启\n"
                              "2. make logs S=scheduler（或 notifier）查看最近的报错（常见：数据库连接失败、磁盘满）\n"
                              "3. 执行 docker compose up -d scheduler notifier 重新拉起；心跳恢复后自动恢复\n"
                              "4. 调度进程停止期间巡检不会执行，恢复后检查“巡检任务”页各任务的最近运行"},
}
AUTH_ERROR = re.compile(r"\[(401|403)\]")
WORKERS = {"scheduler": "调度进程", "notifier": "通知进程"}


def _fp(rule_id: str, key: str) -> str:
    return hashlib.sha1(f"platform|{rule_id}|{key}".encode()).hexdigest()


def _active(db: Session, rule_id: str, key: str) -> AlarmEvent | None:
    return db.scalar(select(AlarmEvent).where(AlarmEvent.fingerprint == _fp(rule_id, key),
                                              AlarmEvent.status.in_(ACTIVE_STATUSES)))


def fallback_recipients(db: Session) -> list[str]:
    cfg = settings_service.get_platform_alarm(db)
    if cfg.mail_to:
        return list(cfg.mail_to)
    admin = db.scalar(select(Role.id).where(Role.code == "admin"))
    return [e for e in db.scalars(select(User.email).where(User.role_id == admin, User.enabled.is_(True),
                                                           User.email.is_not(None))) if e]


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


def on_run(db: Session, run: InspectionRun) -> None:
    """每次任务运行结束后调用（巡检 / 台账同步 / 数据清理）。任何异常只记日志，不影响任务本身。"""
    try:
        _on_run(db, run)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("platform watch failed for run %s", run.id)


def _on_run(db: Session, run: InspectionRun) -> None:
    cfg = settings_service.get_platform_alarm(db)
    key = f"{run.task_code}:{run.tenant_id or 0}"
    task = db.get(InspectionTask, run.task_code)
    task_name = task.name if task else run.task_code
    tenant = _tenant_label(db, run.tenant_id)
    if run.status in ("SUCCESS", "PARTIAL"):
        resolve_alarm(db, "R-PLAT-001", key, f"{task_name}（{tenant}）执行成功，自动恢复")
        if run.tenant_id is not None:
            resolve_alarm(db, "R-PLAT-002", f"tenant:{run.tenant_id}", f"{tenant} 的任务执行成功，认证已恢复")
        return
    if run.status != "FAILED":
        return
    error = (run.error_summary or "")[:300]
    if cfg.auth_fail_alarm and run.tenant_id is not None and AUTH_ERROR.search(error):
        raise_alarm(db, "R-PLAT-002", f"tenant:{run.tenant_id}", name="HCS 接口认证",
                    title=f"认证失败（{task_name}）：{error[:60]}", tenant_id=run.tenant_id,
                    detail={"task": run.task_code, "error": error})
    recent = list(db.scalars(select(InspectionRun.status).where(
        InspectionRun.task_code == run.task_code,
        InspectionRun.tenant_id.is_(None) if run.tenant_id is None else InspectionRun.tenant_id == run.tenant_id,
        InspectionRun.status != "RUNNING").order_by(InspectionRun.started_at.desc(), InspectionRun.id.desc())
        .limit(cfg.task_fail_threshold)))
    if len(recent) >= cfg.task_fail_threshold and all(s == "FAILED" for s in recent):
        raise_alarm(db, "R-PLAT-001", key, name=task_name,
                    title=f"连续 {cfg.task_fail_threshold} 次执行失败：{error[:60]}", tenant_id=run.tenant_id,
                    detail={"task": run.task_code, "error": error, "threshold": cfg.task_fail_threshold})


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
