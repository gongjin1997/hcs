"""子进程运行上下文与数据库提交栅栏；普通单元测试可直接使用原采集引擎。"""
import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models import InspectionRun, TaskExecution, TaskExecutionCancel
from app.models.base import utcnow

from .task_execution import finish

log = logging.getLogger(__name__)


class ExecutionStopped(BaseException):
    """不能被采集器的局部错误兜底吞掉。"""

    def __init__(self, status: str, message: str):
        self.status, self.message = status, message
        super().__init__(message)


@dataclass
class Control:
    execution_id: str
    run_id: int
    owner_token: str
    deadline: float
    params: dict
    emit: Callable[[dict], None]
    done: bool = False
    committing_terminal: bool = False
    last_emit: float = 0
    latest: dict = field(default_factory=dict)
    lock: Any = field(default_factory=threading.Lock)

    def checkpoint(self) -> None:
        if not self.done and time.monotonic() >= self.deadline:
            raise ExecutionStopped("TIMEOUT", "已达到本次巡检执行期限")

    def progress(self, phase: str | None = None, **details) -> None:
        self.checkpoint()
        with self.lock:
            changed = phase is not None and phase != self.latest.get("phase")
            counts_changed = any(key in details and details[key] != self.latest.get(key)
                                 for key in ("current", "total", "unit"))
            message_changed = "message" in details and details["message"] != self.latest.get("message")
            if changed:
                # Counters and query metadata belong to a phase. Keep only run-wide
                # counters when moving on so a new phase cannot inherit an old total.
                self.latest = {key: self.latest[key] for key in ("api_calls", "resource_total", "abnormal_total")
                               if key in self.latest}
            self.latest.update({k: v for k, v in details.items()
                                if isinstance(v, (str, int, float, bool)) or v is None})
            if phase is not None:
                self.latest["phase"] = phase
            now = time.monotonic()
            if changed or counts_changed or message_changed or now - self.last_emit >= .5:
                self.last_emit = now
                self.latest["at"] = utcnow().isoformat()
                log.info("execution_progress_reported", extra={"event": "execution_progress_reported",
                    "task_execution_id": self.execution_id, "run_id": self.run_id,
                    "phase": self.latest.get("phase"), "current": self.latest.get("current"),
                    "total": self.latest.get("total"), "unit": self.latest.get("unit"),
                    "progress_message": self.latest.get("message")})
                try:
                    self.emit(dict(self.latest))
                except (BrokenPipeError, EOFError, OSError):
                    self.emit = lambda _value: None


_control: ContextVar[Control | None] = ContextVar("inspection_execution", default=None)
# 采集器的线程池仍归属于同一个隔离子进程，需继承相同期限。
_process_control: Control | None = None


def current() -> Control | None:
    return _control.get() or _process_control


def progress(phase: str | None = None, **details) -> None:
    control = current()
    if control is not None:
        control.progress(phase, **details)


def new_run(db: Session, **values) -> InspectionRun:
    control = current()
    if control is None:
        direct_run = InspectionRun(**values)
        db.add(direct_run)
        return direct_run
    run = db.get(InspectionRun, control.run_id)
    if run is None or run.task_code != values.get("task_code"):
        raise ExecutionStopped("INTERRUPTED", "执行记录与任务不一致")
    for key, value in values.items():
        if key != "started_at":
            setattr(run, key, value)
    return run


@contextmanager
def managed(db: Session, control: Control, *, process: bool = False):
    global _process_control
    previous = _process_control
    token = _control.set(control)
    if process:
        _process_control = control

    def guard(session: Session) -> None:
        if control.done:
            return
        control.checkpoint()
        if session.in_nested_transaction():
            return  # SAVEPOINT 不是持久提交；外层提交统一取得栅栏。
        row = session.scalar(select(TaskExecution).where(TaskExecution.id == control.execution_id)
                             .execution_options(populate_existing=True).with_for_update(key_share=True))
        now = utcnow()
        if (row is None or row.status != "RUNNING" or row.owner_token != control.owner_token
                or row.lease_expires_at is None or row.lease_expires_at <= now):
            raise ExecutionStopped("INTERRUPTED", "执行器租约已失效，未提交结果")
        if session.get(TaskExecutionCancel, row.id) is not None:
            raise ExecutionStopped("CANCELLED", "已取消本地巡检，未提交本批结果")
        if row.deadline_at is None or row.deadline_at <= now:
            raise ExecutionStopped("TIMEOUT", "已达到本次巡检执行期限，未提交本批结果")
        run = session.get(InspectionRun, control.run_id)
        if run is not None and run.finished_at is not None and run.status not in ("RUNNING", "QUEUED"):
            finish(session, row, run.status, run.error_summary)
            control.committing_terminal = True

    def committed(_session: Session) -> None:
        if control.committing_terminal:
            control.done = True

    def rolled_back(_session: Session) -> None:
        control.committing_terminal = False

    event.listen(db, "before_commit", guard)
    event.listen(db, "after_commit", committed)
    event.listen(db, "after_rollback", rolled_back)
    try:
        yield control
    finally:
        event.remove(db, "before_commit", guard)
        event.remove(db, "after_commit", committed)
        event.remove(db, "after_rollback", rolled_back)
        _control.reset(token)
        _process_control = previous
