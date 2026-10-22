"""调度进程：python -m app.workers.scheduler

- 巡检任务、台账同步（ledger_sync）、数据清理（data_cleanup）：按任务的周期（interval_sec）或 crontab（cron）执行；
  max_instances=1（不重叠）、coalesce（错过的合并为一次）、jitter（打散请求）；每次执行按租户逐个跑（某个租户失败不影响
  其他租户，各自一条运行记录；采集开关关闭的租户跳过）
- 报表：按“报表中心 → 报表设置”（未设置时取环境变量 INSPECTOR_REPORT_CRON）生成日报 / 周报 / 月报并发送
- 每 30 秒同步一次任务配置：页面上修改周期 / crontab / 启停、报表发送时间后无需重启（最迟 30 秒生效）
- 每 30 秒写心跳（含各作业下次执行时间，巡检任务页显示），并检查通知进程心跳（超时产生平台告警 R-PLAT-004）
- 多副本部署时用 Redis 锁保证同一作业同一时刻只在一个副本上执行（docker-compose 单机部署保持 1 个副本即可）
"""
import logging
import signal
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.collectors.registry import TASKS
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.logging import setup_logging
from app.ledger.sync import LEDGER_TASK
from app.models import InspectionTask
from app.reports import service as report_service
from app.services import platform_watch, settings_service
from app.services.cleanup import CLEANUP_TASK
from app.services.pipeline import run_all

log = logging.getLogger("scheduler")
NAME = "scheduler"
INTERNAL = ("_sync", "_beat")
RUNNABLE = {*TASKS, LEDGER_TASK, CLEANUP_TASK}


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


def execute(code: str, ttl: int) -> None:
    def job() -> None:
        with SessionLocal() as db:
            runs = run_all(db, code)
            log.info("task %s finished: %s", code, ", ".join(f"{r.tenant_id or '-'}={r.status}" for r in runs)
                     or "no tenant (all skipped)", extra={"task": code})
    _with_lock(code, ttl, job)


def make_report(kind: str) -> None:
    def job() -> None:
        with SessionLocal() as db:
            reports = report_service.scheduled(db, kind)
            log.info("report %s generated: %s", kind, "、".join(r.title for r in reports))
    _with_lock(f"report:{kind}", 600, job)


def desired_jobs(db) -> dict[str, dict[str, Any]]:
    """数据库里的任务配置 → 期望的调度作业 {job_id: {sig, func, args, name, grace}}。sig 变化时重新调度。"""
    out: dict[str, dict[str, Any]] = {}
    for t in db.scalars(select(InspectionTask).where(InspectionTask.enabled.is_(True))):
        if t.code not in RUNNABLE:
            log.warning("task %s has no collector, skip", t.code)
            continue
        sig = ("cron", t.cron) if t.cron else ("interval", int(t.interval_sec))
        ttl = 3600 if t.cron else max(int(t.interval_sec) * 2, 60)
        out[t.code] = {"sig": sig, "func": execute, "args": [t.code, ttl], "name": t.name,
                       "grace": 3600 if t.cron else 30}
    rpt = settings_service.get_report(db)
    for kind in settings_service.REPORT_KINDS:
        cron = rpt.cron.get(kind)
        if cron and rpt.enabled.get(kind, True):
            out[f"report_{kind.lower()}"] = {"sig": ("cron", cron), "func": make_report, "args": [kind],
                                             "name": f"report {kind}", "grace": 3600}
    return out


class JobSync:
    """把数据库配置同步到 APScheduler（新增 / 删除 / 修改周期），供调度进程每 30 秒调用一次。"""

    def __init__(self, sched: BlockingScheduler, tz: str) -> None:
        self.sched, self.tz = sched, tz
        self.sigs: dict[str, tuple] = {}

    def trigger(self, sig: tuple, jitter: bool = True):
        if sig[0] == "cron":
            return CronTrigger.from_crontab(sig[1], timezone=self.tz)
        return IntervalTrigger(seconds=sig[1], jitter=min(10, sig[1] // 6) if jitter else None, timezone=self.tz)

    def sync(self) -> dict[str, list[str]]:
        changes: dict[str, list[str]] = {"added": [], "removed": [], "updated": []}
        with SessionLocal() as db:
            want = desired_jobs(db)
        for jid in [j for j in self.sigs if j not in want]:
            self.sched.remove_job(jid)
            self.sigs.pop(jid)
            changes["removed"].append(jid)
        for jid, j in want.items():
            if self.sigs.get(jid) == j["sig"]:
                continue
            try:
                trig = self.trigger(j["sig"])
            except ValueError:
                log.error("invalid schedule for %s: %s", jid, j["sig"])
                continue
            if jid in self.sigs:
                self.sched.modify_job(jid, args=j["args"], name=j["name"])
                self.sched.reschedule_job(jid, trigger=trig)
                changes["updated"].append(jid)
            else:
                self.sched.add_job(j["func"], trig, args=j["args"], id=jid, name=j["name"],
                                   misfire_grace_time=j["grace"])
                changes["added"].append(jid)
            self.sigs[jid] = j["sig"]
        if any(changes.values()):
            log.info("jobs synced: %s", {k: v for k, v in changes.items() if v})
        return changes

    def next_runs(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for j in self.sched.get_jobs():
            if j.id not in INTERNAL:
                nrt = getattr(j, "next_run_time", None)       # 调度器启动前的作业还没有下次执行时间
                out[j.id] = nrt.isoformat() if nrt else None
        return out


def main() -> None:
    setup_logging()
    s = get_settings()
    sched = BlockingScheduler(timezone=s.timezone,
                              job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30})
    jobs = JobSync(sched, s.timezone)
    jobs.sync()

    def sync() -> None:
        try:
            jobs.sync()
        except Exception:  # noqa: BLE001
            log.exception("job sync failed")

    def beat() -> None:
        try:
            with SessionLocal() as db:
                platform_watch.beat(db, NAME, {"jobs": jobs.next_runs()})
                platform_watch.check_heartbeats(db, ["notifier"])
        except Exception:  # noqa: BLE001
            log.exception("heartbeat failed")

    sched.add_job(sync, "interval", seconds=30, id="_sync", name="sync jobs")
    sched.add_job(beat, "interval", seconds=30, id="_beat", name="heartbeat",
                  next_run_time=datetime.now(ZoneInfo(s.timezone)) + timedelta(seconds=3))
    signal.signal(signal.SIGTERM, lambda *_: sched.shutdown(wait=False))
    log.info("scheduler started with %d jobs", len(jobs.sigs))
    sched.start()


if __name__ == "__main__":
    main()
