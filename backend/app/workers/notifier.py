"""通知发送进程：python -m app.workers.notifier

- 每 5 秒轮询 Outbox 发送邮件（可多副本，PostgreSQL 下 SKIP LOCKED 并行）
- 每分钟：未认领告警按路由升级规则通知组长（app.notify.escalation）；静默 / 维护窗口生效与结束（结束后仍未恢复的告警补发通知）；
  检查调度进程心跳（超时产生平台告警 R-PLAT-004，与调度进程互相看护）
- 每 30 秒写一次心跳（平台状态页、/metrics 使用）
"""
import logging
import signal
import time

from sqlalchemy import func, select

from app.core.config import ensure_prod_ready
from app.core.db import SessionLocal
from app.core.logging import setup_logging
from app.models import Notification
from app.notify.dispatcher import dispatch_pending
from app.notify.escalation import escalate_unacked
from app.services import platform_watch, silence_service

log = logging.getLogger("notifier")
NAME = "notifier"
_stop = False


def _graceful(*_: object) -> None:
    global _stop
    _stop = True
    log.info("notifier stopping")


def every_minute(db) -> None:
    if (e := escalate_unacked(db)):
        log.info("escalated %s unacknowledged alarms", e)
    res = silence_service.sweep(db)
    if res["released"] or res["suppressed"]:
        log.info("silence sweep: released=%s suppressed=%s", res["released"], res["suppressed"])
    platform_watch.check_heartbeats(db, ["scheduler"])


def heartbeat(db) -> None:
    pending = db.scalar(select(func.count()).select_from(Notification).where(
        Notification.status.in_(("PENDING", "RETRYING")))) or 0
    platform_watch.beat(db, NAME, {"pending": int(pending)})


def main(interval: float = 5.0, minute: float = 60.0, beat_every: float = 30.0) -> None:
    setup_logging()
    ensure_prod_ready()
    signal.signal(signal.SIGTERM, _graceful)
    log.info("notifier started")
    last_minute = last_beat = 0.0
    while not _stop:
        try:
            with SessionLocal() as db:
                if time.monotonic() - last_beat >= beat_every:
                    last_beat = time.monotonic()
                    heartbeat(db)
                if time.monotonic() - last_minute >= minute:
                    last_minute = time.monotonic()
                    every_minute(db)
                n = dispatch_pending(db)
                if n:
                    log.info("sent %s notifications", n)
        except Exception:  # noqa: BLE001
            log.exception("notifier loop failed")
        time.sleep(interval)


if __name__ == "__main__":
    main()
