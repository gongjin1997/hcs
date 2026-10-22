"""调度进程：python -m app.workers.scheduler

- 巡检任务 + 台账同步（ledger_sync）：每个任务一个 interval 作业，max_instances=1（不重叠）、coalesce（错过的合并为一次）、
  jitter（打散请求）；每次执行按租户逐个跑（某个租户失败不影响其他租户，各自一条运行记录）
- 报表：按 INSPECTOR_REPORT_CRON 生成日报 / 周报 / 月报（合并报表 + 每个启用租户一份）；配置了收件人时自动发送
- 多副本部署时用 Redis 锁保证同一作业同一时刻只在一个副本上执行
"""
import logging
from collections.abc import Callable

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.collectors.registry import TASKS
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.logging import setup_logging
from app.ledger.sync import LEDGER_TASK
from app.models import InspectionTask
from app.reports import service as report_service
from app.services.pipeline import run_all

log = logging.getLogger("scheduler")


def _locked(code: str, ttl: int):
    url = get_settings().redis_url
    if not url:
        return None
    import redis

    client = redis.Redis.from_url(url)
    lock = client.lock(f"inspector:lock:{code}", timeout=ttl, blocking=False)
    return lock if lock.acquire() else False


def _with_lock(code: str, ttl: int, fn: Callable[[], None]) -> None:
    lock = _locked(code, ttl)
    if lock is False:
        log.info("job %s is running on another replica, skip", code, extra={"task": code})
        return
    try:
        fn()
    finally:
        if lock:
            lock.release()


def execute(code: str, interval: int) -> None:
    def job() -> None:
        with SessionLocal() as db:
            runs = run_all(db, code)
            log.info("task %s finished: %s", code, ", ".join(f"{r.tenant_id or '-'}={r.status}" for r in runs) or "no tenant",
                     extra={"task": code})
    _with_lock(code, max(interval * 2, 60), job)


def make_report(kind: str) -> None:
    def job() -> None:
        with SessionLocal() as db:
            reports = report_service.scheduled(db, kind)
            log.info("report %s generated: %s", kind, "、".join(r.title for r in reports))
    _with_lock(f"report:{kind}", 600, job)


def main() -> None:
    setup_logging()
    s = get_settings()
    sched = BlockingScheduler(timezone=s.timezone,
                              job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30})
    with SessionLocal() as db:
        tasks = db.scalars(select(InspectionTask).where(InspectionTask.enabled.is_(True))).all()
    for t in tasks:
        if t.code not in TASKS and t.code != LEDGER_TASK:
            log.warning("task %s has no collector, skip", t.code)
            continue
        sched.add_job(execute, "interval", seconds=t.interval_sec, jitter=min(10, t.interval_sec // 6),
                      args=[t.code, t.interval_sec], id=t.code, name=t.name)
        log.info("scheduled %s every %ss", t.code, t.interval_sec)
    for kind, cron in s.report_cron.items():
        if cron:
            sched.add_job(make_report, CronTrigger.from_crontab(cron, timezone=s.timezone), args=[kind],
                          id=f"report_{kind.lower()}", name=f"report {kind}", misfire_grace_time=3600)
            log.info("scheduled report %s at '%s'", kind, cron)
    sched.start()


if __name__ == "__main__":
    main()
