"""通知发送进程：python -m app.workers.notifier

- 轮询 Outbox 发送邮件（生产可多副本，PostgreSQL 下 SKIP LOCKED 并行）
- 每分钟检查一次未认领告警，到点的按路由升级规则通知组长（app.notify.escalation）
"""
import logging
import time

from app.core.db import SessionLocal
from app.core.logging import setup_logging
from app.notify.dispatcher import dispatch_pending
from app.notify.escalation import escalate_unacked

log = logging.getLogger("notifier")


def main(interval: float = 5.0, escalate_every: float = 60.0) -> None:
    setup_logging()
    log.info("notifier started")
    last_escalate = 0.0
    while True:
        try:
            with SessionLocal() as db:
                if time.monotonic() - last_escalate >= escalate_every:
                    last_escalate = time.monotonic()
                    if (e := escalate_unacked(db)):
                        log.info("escalated %s unacknowledged alarms", e)
                n = dispatch_pending(db)
                if n:
                    log.info("sent %s notifications", n)
        except Exception:  # noqa: BLE001
            log.exception("dispatch failed")
        time.sleep(interval)


if __name__ == "__main__":
    main()
