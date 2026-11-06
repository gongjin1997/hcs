"""调度进程：python -m app.workers.scheduler

- 巡检任务、台账同步（ledger_sync）、数据清理（data_cleanup）：按任务的周期（interval_sec）或 crontab（cron）执行；
  max_instances=1（不重叠）、coalesce（错过的合并为一次）、jitter（打散请求）；每次执行按租户逐个跑（某个租户失败不影响
  其他租户，各自一条运行记录；采集开关关闭的租户跳过）
- 报表：按“系统管理 → 巡检任务 → 修改发送时间 / 收件人”（未设置时取环境变量 INSPECTOR_REPORT_CRON）生成日报 / 周报 / 月报并发送
- 每 30 秒同步一次任务配置：页面上修改周期 / crontab / 启停、报表发送时间后无需重启（最迟 30 秒生效）
- 每 30 秒写心跳（含各作业下次执行时间，巡检任务页显示），并检查通知进程心跳（超时产生平台告警 R-PLAT-004）
- 多副本部署时用 Redis 锁保证同一作业同一时刻只在一个副本上执行（docker-compose 单机部署保持 1 个副本即可）
"""
import logging
import signal
from collections import Counter
from collections.abc import Callable
from contextvars import copy_context
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Event, Lock, Thread
from time import perf_counter
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.collectors.registry import TASKS
from app.core.config import ensure_prod_ready, get_settings
from app.core.db import SessionLocal
from app.core.logging import log_context, setup_logging
from app.ledger.sync import LEDGER_TASK
from app.ledger.sync import sync_all as sync_inventory
from app.models import InspectionTask, SysSetting
from app.reports import service as report_service
from app.services import collection_policy, platform_watch, settings_service
from app.services.cleanup import CLEANUP_TASK
from app.services.pipeline import run_all

log = logging.getLogger("scheduler")
NAME = "scheduler"
INTERNAL = ("_sync", "_beat")
RUNNABLE = {*TASKS, LEDGER_TASK, CLEANUP_TASK}
_running: set[str] = set()
_running_guard = Lock()


def _log_error(event: str, exc: Exception, **fields: object) -> None:
    log.error(event, extra={"event": event, "exception_type": type(exc).__name__, **fields},
              exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))


def _locked(code: str, ttl: int):
    url = get_settings().redis_url
    if not url:
        return None
    import redis

    client = redis.Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5)
    lock = client.lock(f"inspector:lock:{code}", timeout=ttl, blocking=False, thread_local=False)
    try:
        if lock.acquire():
            return lock
    except Exception:
        client.close()
        raise
    client.close()
    return False


def _with_lock(code: str, ttl: int, fn: Callable[[], None]) -> None:
    started = perf_counter()
    with _running_guard:
        if code in _running:
            log.debug("job_skipped", extra={"event": "job_skipped", "task": code, "reason": "local_overlap"})
            return
        _running.add(code)
    with log_context(task=code, job_id=uuid4().hex):
        try:
            _run_locked(code, ttl, fn)
        except Exception as exc:
            _log_error("job_failed", exc, cost_ms=round((perf_counter() - started) * 1000, 2))
            raise
        finally:
            with _running_guard:
                _running.discard(code)


def _run_locked(code: str, ttl: int, fn: Callable[[], None]) -> None:
    lock = _locked(code, ttl)
    if lock is False:
        log.debug("job_skipped", extra={"event": "job_skipped", "reason": "replica_overlap"})
        return
    stopped = Event()
    renewer = None
    if lock:
        def renew() -> None:
            while not stopped.wait(max(0.05, ttl / 3)):
                try:
                    lock.extend(ttl, replace_ttl=True)
                except Exception as exc:
                    _log_error("job_lock_renewal_failed", exc)
                    return
        context = copy_context()
        renewer = Thread(target=lambda: context.run(renew), name=f"job-lock-{code}", daemon=True)
        renewer.start()
    try:
        fn()
    finally:
        stopped.set()
        if renewer:
            renewer.join(timeout=10)
        if lock:
            try:
                lock.release()
            except Exception as exc:
                _log_error("job_lock_release_failed", exc)
            finally:
                try:
                    lock.redis.close()
                except Exception as exc:
                    _log_error("job_lock_connection_close_failed", exc)


def execute(code: str, ttl: int) -> None:
    def job() -> None:
        started = perf_counter()
        with SessionLocal() as db:
            runs = run_all(db, code)
            statuses = dict(Counter(r.status for r in runs))
            outcome = platform_watch.run_outcome(runs)
            level = logging.WARNING if outcome not in ("SUCCESS", "SKIPPED") else logging.INFO
            log.log(level if runs else logging.DEBUG, "job_finished", extra={
                "event": "job_finished", "cost_ms": round((perf_counter() - started) * 1000, 2),
                "target_count": len(runs), "statuses": statuses, "outcome": outcome, "run_ids": [r.id for r in runs],
                "scopes": [{"tenant_id": r.tenant_id, "project_ref": getattr(r, "project_ref", None),
                            "status": r.status} for r in runs],
                "resource_count": sum(r.resource_total for r in runs), "api_calls": sum(r.api_calls for r in runs)})
    _with_lock(code, ttl, job)


def execute_ecs_inventory() -> None:
    def job() -> None:
        with SessionLocal() as db:
            # The legacy ledger job retains its original interval/cron for all other types.
            sync_inventory(db, types=[collection_policy.RESOURCE_TYPE])
    _with_lock("ecs_inventory", 900, job)


def make_report(kind: str) -> None:
    def job() -> None:
        started = perf_counter()
        log.info("report_job_started", extra={"event": "report_job_started", "report_kind": kind})
        with SessionLocal() as db:
            reports = report_service.scheduled(db, kind)
            log.info("report_job_finished", extra={"event": "report_job_finished", "report_kind": kind,
                     "report_count": len(reports), "report_ids": [r.id for r in reports],
                     "cost_ms": round((perf_counter() - started) * 1000, 2)})
    _with_lock(f"report:{kind}", 600, job)


def desired_jobs(db) -> dict[str, dict[str, Any]]:
    """数据库里的任务配置 → 期望的调度作业 {job_id: {sig, func, args, name, grace}}。sig 变化时重新调度。"""
    out: dict[str, dict[str, Any]] = {}
    policy_ticks = {phase: 60 for phase in collection_policy.PHASES}
    for value in db.scalars(select(SysSetting.value).where(SysSetting.key.like("collection_policy:%:ecs_server"))):
        if not isinstance(value, dict):
            continue
        for phase in policy_ticks:
            stage = value.get(phase)
            interval = stage.get("interval_sec", 60) if isinstance(stage, dict) else 60
            if type(interval) is int and interval >= 30:
                policy_ticks[phase] = min(policy_ticks[phase], interval)
    for t in db.scalars(select(InspectionTask).where(InspectionTask.enabled.is_(True))):
        if t.code not in RUNNABLE:
            log.warning("task %s has no collector, skip", t.code)
            continue
        policy_task = t.code in collection_policy.TASK_PHASES
        tick = min(60, max(30, int(t.interval_sec)), policy_ticks[collection_policy.TASK_PHASES[t.code]]) if policy_task else 60
        sig = ("interval", tick) if policy_task else (("cron", t.cron) if t.cron else ("interval", int(t.interval_sec)))
        ttl = 3600 if t.cron else max(int(t.interval_sec) * 2, 60)
        out[t.code] = {"sig": sig, "func": execute, "args": [t.code, ttl], "name": t.name,
                       "grace": 3600 if t.cron else 30}
        if t.code == LEDGER_TASK:
            out["ecs_inventory"] = {"sig": ("interval", policy_ticks["inventory"]), "func": execute_ecs_inventory, "args": [],
                                    "name": "ECS inventory policy", "grace": 30}
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

    def trigger(self, sig: tuple, jitter: bool = True, job_id: str | None = None):
        if sig[0] == "cron":
            return CronTrigger.from_crontab(sig[1], timezone=self.tz)
        if job_id is not None:
            # 稳定相位只改变 interval 作业的起点，不漂移周期，不改变 cron 的发送时刻。
            phase_ms = int.from_bytes(sha256(job_id.encode()).digest()[:8], "big") % max(1, int(sig[1] * 1000))
            return IntervalTrigger(seconds=sig[1], start_date=datetime(1970, 1, 1, tzinfo=UTC)
                                   + timedelta(milliseconds=phase_ms), timezone=self.tz)
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
                trig = self.trigger(j["sig"], job_id=jid)
            except ValueError:
                log.error("invalid schedule for %s: %s", jid, j["sig"])
                continue
            if jid in self.sigs:
                self.sched.modify_job(jid, args=j["args"], name=j["name"], max_instances=1, coalesce=True)
                self.sched.reschedule_job(jid, trigger=trig)
                changes["updated"].append(jid)
            else:
                self.sched.add_job(j["func"], trig, args=j["args"], id=jid, name=j["name"],
                                   misfire_grace_time=j["grace"], max_instances=1, coalesce=True)
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
    setup_logging(service=NAME)
    ensure_prod_ready()
    s = get_settings()
    sched = BlockingScheduler(timezone=s.timezone,
                              job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30})
    jobs = JobSync(sched, s.timezone)
    jobs.sync()

    def sync() -> None:
        try:
            jobs.sync()
        except Exception as exc:  # noqa: BLE001
            _log_error("job_sync_failed", exc)

    def beat() -> None:
        try:
            with SessionLocal() as db:
                platform_watch.beat(db, NAME, {"jobs": jobs.next_runs()})
                platform_watch.check_heartbeats(db, ["notifier"])
        except Exception as exc:  # noqa: BLE001
            _log_error("scheduler_heartbeat_failed", exc)

    sched.add_job(sync, "interval", seconds=30, id="_sync", name="sync jobs")
    sched.add_job(beat, "interval", seconds=30, id="_beat", name="heartbeat",
                  next_run_time=datetime.now(ZoneInfo(s.timezone)) + timedelta(seconds=3))
    signal.signal(signal.SIGTERM, lambda *_: sched.shutdown(wait=False))
    log.info("scheduler_started", extra={"event": "scheduler_started", "job_count": len(jobs.sigs)})
    try:
        sched.start()
    finally:
        log.info("scheduler_stopped", extra={"event": "scheduler_stopped"})


if __name__ == "__main__":
    main()
