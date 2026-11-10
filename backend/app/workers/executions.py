"""现有 scheduler 内的执行监督器；每次巡检使用可回收的独立子进程。"""
import logging
import multiprocessing
import os
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from threading import Event, Thread, Timer
from uuid import uuid4

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal, engine
from app.core.logging import log_context, setup_logging
from app.hcs.ratelimit import request_budget
from app.models import InspectionTask, TaskExecution, TaskExecutionCancel
from app.models.base import utcnow
from app.services import execution_runtime as runtime
from app.services import task_execution as queue
from app.services.catalog import CLEANUP_TASK_CODE
from app.services.tenancy import resolve_targets

log = logging.getLogger(__name__)


def _finish_owned(execution_id: str, token: str, status: str, message: str, code: str | None = None) -> None:
    with SessionLocal() as db:
        row = db.scalar(select(TaskExecution).where(TaskExecution.id == execution_id).with_for_update())
        if row is not None and row.owner_token == token:
            queue.finish(db, row, status, message, code=code)
            db.commit()


def _run_child(execution_id: str, token: str, sender) -> None:
    setup_logging(service="inspection")
    # spawn 不继承连接；dispose 也防止嵌入调用时误用父进程连接。
    engine.dispose()
    timer = None
    stopped = Event()
    parent_pid = os.getppid()

    def watch_parent() -> None:
        while not stopped.wait(.5):
            if os.getppid() != parent_pid:
                os._exit(126)  # 监督器被 SIGKILL 时不留下继续采集的孤儿进程。

    Thread(target=watch_parent, name="inspection-parent-watch", daemon=True).start()
    with log_context(task_execution_id=execution_id):
        try:
            with SessionLocal() as db:
                row = db.get(TaskExecution, execution_id)
                if row is None or row.status != "RUNNING" or row.owner_token != token:
                    return
                seconds = max(0, (row.deadline_at - utcnow()).total_seconds()) if row.deadline_at else 0
                # 即使监督器崩溃、网络库卡死或数据库不可用，子进程也不能无限存活。
                timer = Timer(seconds, lambda: os._exit(124))
                timer.daemon = True
                timer.start()
                control = runtime.Control(row.id, row.run_id, token, time.monotonic() + seconds,
                                          dict(row.params or {}), sender.send)
                with runtime.managed(db, control, process=True), request_budget(attempts=50000, seconds=seconds):
                    task = db.get(InspectionTask, row.task_code)
                    if task is None or not task.enabled:
                        raise runtime.ExecutionStopped("SKIPPED", "任务已关闭")
                    target = None
                    if row.task_code != CLEANUP_TASK_CODE:
                        target = next((t for t in resolve_targets(db,
                            [row.tenant_id] if row.tenant_id is not None else None)
                            if (t.tenant_id, t.project_ref) == (row.tenant_id, row.project_ref)), None)
                        if target is None or not queue.target_enabled(db, row.task_code, row.tenant_id, row.project_ref):
                            raise runtime.ExecutionStopped("SKIPPED", "租户、项目或采集开关已关闭")
                    runtime.progress("collecting", message="开始采集")
                    from app.services.pipeline import run_task
                    run = run_task(db, row.task_code, target=target, trigger=row.trigger_type)
                    if not control.done:
                        # 任何引擎路径都必须结束同一条入队运行记录。
                        if run.id != row.run_id:
                            raise runtime.ExecutionStopped("FAILED", "任务返回了不同的运行记录")
                        db.commit()
        except runtime.ExecutionStopped as exc:
            _finish_owned(execution_id, token, exc.status, exc.message, f"LOCAL.{exc.status}")
        except BaseException as exc:
            log.error("execution_worker_failed", extra={"event": "execution_worker_failed",
                      "task_execution_id": execution_id, "exception_type": type(exc).__name__},
                      exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
            _finish_owned(execution_id, token, "FAILED", f"执行失败（{type(exc).__name__}），请查看执行日志",
                          "LOCAL.WORKER_ERROR")
        finally:
            stopped.set()
            if timer is not None:
                timer.cancel()
            sender.close()


@dataclass
class Child:
    process: BaseProcess
    receiver: Connection
    token: str
    last_heartbeat: float = 0
    pending: deque[dict] = field(default_factory=lambda: deque(maxlen=100))
    terminal_seen: float | None = None


def _record_progress(row: TaskExecution, progress: dict, *, now) -> bool:
    previous = row.progress if isinstance(row.progress, dict) else {}
    changed = any(progress.get(key) != previous.get(key)
                  for key in ("phase", "current", "total", "unit", "message", "cluster_id", "query"))
    if changed:
        history = list(row.history or [])
        event = {**progress, "at": progress.get("at") or now.isoformat()}
        if row.status in queue.ACTIVE or not history or history[-1].get("phase") != "finished":
            history = [*history[-99:], event]
        else:
            history = [*history[-100:-1], event, history[-1]]
        row.history = history
    if row.status in queue.ACTIVE:
        row.progress = progress
    return changed


def _log_progress(row: TaskExecution, progress: dict) -> None:
    log.info("execution_progress_received", extra={"event": "execution_progress_received", "task_execution_id": row.id,
             "run_id": row.run_id, "task": row.task_code, "tenant_id": row.tenant_id,
             "project_ref": row.project_ref, "phase": progress.get("phase"),
             "current": progress.get("current"), "total": progress.get("total"),
             "unit": progress.get("unit"), "progress_message": progress.get("message")})


def _flush_pending_progress(row: TaskExecution, pending: deque[dict], *, now) -> dict | None:
    latest = None
    for progress in pending:
        if _record_progress(row, progress, now=now):
            _log_progress(row, progress)
        latest = progress
    pending.clear()
    return latest


class Supervisor:
    def __init__(self, *, concurrency: int | None = None):
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"
        self.concurrency = concurrency or (2 if get_settings().redis_url and engine.dialect.name != "sqlite" else 1)
        self.children: dict[str, Child] = {}
        self.stopped = Event()
        self.thread: Thread | None = None
        self.context = multiprocessing.get_context("spawn")

    def start(self) -> None:
        # 升级时 deploy 先停止旧 API/scheduler，故这些无执行租约的旧记录不可能继续运行。
        with SessionLocal() as db:
            recovered = queue.reap(db, legacy_before=utcnow())
        log.info("execution_supervisor_started", extra={"event": "execution_supervisor_started",
                 "worker_id": self.worker_id, "concurrency": self.concurrency, "recovered": recovered})
        self.thread = Thread(target=self._loop, name="inspection-supervisor", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        try:
            while not self.stopped.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    log.error("execution_supervisor_tick_failed", extra={"event": "execution_supervisor_tick_failed",
                              "exception_type": type(exc).__name__},
                              exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
                self.stopped.wait(.5)
        finally:
            for execution_id, child in list(self.children.items()):
                self._stop_child(child)
                try:
                    _finish_owned(execution_id, child.token, "INTERRUPTED", "调度服务已停止，请重新执行",
                                  "LOCAL.SHUTDOWN")
                except Exception:
                    log.exception("execution_shutdown_record_failed")
                self._release(execution_id)

    @staticmethod
    def _stop_child(child: Child) -> None:
        if child.process.is_alive():
            child.process.terminate()
            child.process.join(timeout=1)
        if child.process.is_alive():
            child.process.kill()
            child.process.join(timeout=1)

    def _release(self, execution_id: str) -> None:
        child = self.children.pop(execution_id)
        child.receiver.close()
        child.process.join(timeout=.1)
        child.process.close()

    def tick(self) -> None:
        for execution_id, child in list(self.children.items()):
            try:
                while child.receiver.poll():
                    child.pending.append(child.receiver.recv())
            except EOFError:
                pass
            with SessionLocal() as db:
                row = db.scalar(select(TaskExecution).where(TaskExecution.id == execution_id)
                    .with_for_update(skip_locked=True))
                if row is None:  # 正在提交结果的子进程持有栅栏；下一轮再检查。
                    continue
                now = utcnow()
                reason: tuple[str, str, str] | None = None
                if row.status not in queue.ACTIVE or row.owner_token != child.token:
                    if row.owner_token == child.token and child.pending:
                        final_message = (row.progress or {}).get("message") or row.status
                        latest = _flush_pending_progress(row, child.pending, now=now)
                        assert latest is not None
                        row.progress = {**latest, "phase": "finished", "phase_label": "执行结束",
                                        "message": final_message}
                        db.commit()
                    # 引擎已原子提交终态后，留出短时间完成平台告警与日志收尾。
                    if row.status in ("SUCCESS", "PARTIAL", "FAILED", "SKIPPED") and child.process.is_alive():
                        child.terminal_seen = child.terminal_seen or time.monotonic()
                        if time.monotonic() - child.terminal_seen < 3:
                            continue
                    self._stop_child(child)
                    self._release(execution_id)
                    continue
                if db.get(TaskExecutionCancel, row.id) is not None:
                    reason = ("CANCELLED", "已取消本地巡检；云作业继续运行", "LOCAL.CANCELLED")
                elif row.deadline_at is None or row.deadline_at <= now or child.process.exitcode == 124:
                    reason = ("TIMEOUT", "已达到本次巡检执行期限", "LOCAL.DEADLINE")
                elif row.lease_expires_at is None or row.lease_expires_at <= now:
                    reason = ("INTERRUPTED", "执行器租约已失效，请重新执行", "LOCAL.LEASE_LOST")
                elif not child.process.is_alive():
                    reason = ("INTERRUPTED", "执行子进程意外退出，请查看日志后重试", "LOCAL.PROCESS_EXIT")
                if reason:
                    self._stop_child(child)
                    queue.finish(db, row, reason[0], reason[1], code=reason[2])
                    db.commit()
                    self._release(execution_id)
                    continue
                if child.pending:
                    _flush_pending_progress(row, child.pending, now=now)
                if time.monotonic() - child.last_heartbeat >= 5:
                    row.heartbeat_at = now
                    row.lease_expires_at = min(row.deadline_at or now, now + timedelta(seconds=queue.LEASE_SECONDS))
                    child.last_heartbeat = time.monotonic()
                db.commit()
        with SessionLocal() as db:
            queue.reap(db)
        while len(self.children) < self.concurrency and not self.stopped.is_set():
            with SessionLocal() as db:
                row = queue.claim_next(db, self.worker_id)
                if row is None:
                    break
                execution_id, token = row.id, row.owner_token or ""
            receiver, sender = self.context.Pipe(duplex=False)
            process = self.context.Process(target=_run_child, args=(execution_id, token, sender),
                                           name=f"inspection-{execution_id[:8]}")
            try:
                process.start()
            except Exception:
                receiver.close()
                sender.close()
                _finish_owned(execution_id, token, "FAILED", "无法启动执行子进程", "LOCAL.SPAWN_FAILED")
                raise
            sender.close()
            self.children[execution_id] = Child(process, receiver, token)

    def close(self) -> None:
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=10)
