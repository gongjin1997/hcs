"""通知发送进程：python -m app.workers.notifier

- 每 5 秒轮询 Outbox 发送邮件（可多副本，PostgreSQL 下 SKIP LOCKED 并行）
- 每分钟：未认领告警按路由升级规则通知组长（app.notify.escalation）；静默 / 维护窗口生效与结束（结束后仍未恢复的告警补发通知）；
  检查调度进程心跳（超时产生平台告警 R-PLAT-004，与调度进程互相看护）
- 每 30 秒写一次心跳（平台状态页、/metrics 使用）
"""
import logging
import signal
import time
from uuid import uuid4

from sqlalchemy import func, select

from app.core.config import ensure_prod_ready
from app.core.db import SessionLocal
from app.core.logging import log_context, setup_logging
from app.models import Notification
from app.notify.dispatcher import dispatch_pending
from app.notify.escalation import escalate_unacked
from app.services import platform_watch, silence_service, sms_service

log = logging.getLogger("notifier")
NAME = "notifier"
_stop = False


def _graceful(*_: object) -> None:
    global _stop
    _stop = True
    log.info("notifier stopping")


def every_minute(db) -> None:
    escalate_unacked(db)
    res = silence_service.sweep(db)
    if res["released"] or res["suppressed"]:
        log.info("silence_sweep_finished", extra={"event": "silence_sweep_finished", **res})
    sms_service.enqueue_due(db)
    platform_watch.check_heartbeats(db, ["scheduler"])


def heartbeat(db) -> None:
    pending = db.scalar(select(func.count()).select_from(Notification).where(
        Notification.status.in_(("PENDING", "RETRYING")))) or 0
    platform_watch.beat(db, NAME, {"pending": int(pending)})


def main(interval: float = 5.0, minute: float = 60.0, beat_every: float = 30.0) -> None:
    setup_logging(service=NAME)
    ensure_prod_ready()
    signal.signal(signal.SIGTERM, _graceful)
    log.info("notifier_started", extra={"event": "notifier_started"})
    last_minute = last_beat = 0.0
    while not _stop:
        with log_context(operation_id=uuid4().hex, task="notification_dispatch"):
            phase = "heartbeat"
            try:
                with SessionLocal() as db:
                    if time.monotonic() - last_beat >= beat_every:
                        last_beat = time.monotonic()
                        heartbeat(db)
                    phase = "maintenance"
                    if time.monotonic() - last_minute >= minute:
                        last_minute = time.monotonic()
                        every_minute(db)
                    phase = "dispatch"
                    dispatch_pending(db)
                    phase = "sms_dispatch"
                    sms_service.dispatch_pending(db)
            except Exception as exc:  # noqa: BLE001
                log.error("notifier_cycle_failed", extra={"event": "notifier_cycle_failed", "phase": phase,
                          "exception_type": type(exc).__name__},
                          exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
        time.sleep(interval)
    log.info("notifier_stopped", extra={"event": "notifier_stopped"})


if __name__ == "__main__":
    main()
