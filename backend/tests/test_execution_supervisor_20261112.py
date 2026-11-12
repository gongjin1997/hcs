"""执行监督器与子进程入口的进程内回归：期限保护不依赖数据库，缺失或被锁的记录不占槽位。"""
import time
from collections import deque
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.models import InspectionRun, InspectionTask, TaskExecution
from app.models.base import utcnow
from app.services import task_execution as queue
from app.workers import executions


class FakeProcess:
    def __init__(self, *, alive=True, exitcode=None):
        self.alive, self.exitcode = alive, exitcode
        self.signals: list[str] = []
        self.closed = False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.signals.append("terminate")
        self.alive, self.exitcode = False, -15

    def kill(self):
        self.signals.append("kill")
        self.alive, self.exitcode = False, -9

    def join(self, timeout=None):
        pass

    def close(self):
        self.closed = True


class FakePipeEnd:
    def __init__(self, events=()):
        self.events = deque(events)
        self.closed = False

    def poll(self):
        return bool(self.events)

    def recv(self):
        return self.events.popleft()

    def send(self, event):
        self.events.append(event)

    def close(self):
        self.closed = True


class FakeContext:
    """替代 spawn 上下文：记录启动参数，不创建真实进程。"""

    def __init__(self, *, start_error: Exception | None = None):
        self.started: list[tuple] = []
        self.start_error = start_error

    def Pipe(self, duplex=False):  # noqa: N802
        return FakePipeEnd(), FakePipeEnd()

    def Process(self, target, args, name):  # noqa: N802
        process = FakeProcess(alive=False)
        if self.start_error is not None:
            process.start = self._fail
        self.started.append((target, args, name, process))
        return process

    def _fail(self):
        raise self.start_error


class RecordingTimer:
    """替代 threading.Timer：测试进程内不能真的执行 os._exit。"""

    created: list["RecordingTimer"]

    def __init__(self, seconds, callback):
        self.seconds, self.daemon, self.started, self.cancelled = seconds, False, False, False
        self.created.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


@pytest.fixture()
def timers(monkeypatch):
    created: list[RecordingTimer] = []
    monkeypatch.setattr(RecordingTimer, "created", created, raising=False)
    monkeypatch.setattr(executions, "Timer", RecordingTimer)
    return created


@pytest.fixture()
def child_process(monkeypatch, timers):
    """让 _run_child 能在测试进程内运行：不改全局日志，不动共享连接池。"""
    monkeypatch.setattr(executions, "setup_logging", lambda **_: None)
    monkeypatch.setattr(executions, "engine", SimpleNamespace(dispose=lambda: None))
    return timers


@pytest.fixture()
def sessions(mem_db, monkeypatch):
    factory = sessionmaker(bind=mem_db.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(executions, "SessionLocal", factory)
    return factory


@pytest.fixture()
def supervisor(sessions):
    worker = executions.Supervisor(concurrency=1)
    worker.context = FakeContext()
    return worker


def enqueue(db, code="dcs_status", *, timeout_sec=60):
    if db.get(InspectionTask, code) is None:
        db.add(InspectionTask(code=code, name=code, service=code.split("_")[0], interval_sec=120,
                              enabled=True, params={"timeout_sec": timeout_sec}))
        db.commit()
    return queue.enqueue(db, code)["execution_ids"][0]


def running(db, supervisor, **kwargs):
    """入队并由监督器领取；返回执行编号与对应的（假）子进程记录。"""
    execution_id = enqueue(db, **kwargs)
    supervisor.tick()
    return execution_id, supervisor.children[execution_id]


def reload(db, execution_id):
    db.expire_all()
    return db.get(TaskExecution, execution_id)


def far():
    return time.monotonic() + 3600


def test_exited_child_of_missing_execution_releases_its_slot(mem_db, supervisor):
    # 复核 P2：记录被删除后，已退出的子进程不能永久占用唯一的执行槽位。
    process, receiver = FakeProcess(alive=False, exitcode=124), FakePipeEnd()
    supervisor.children["deleted-execution"] = executions.Child(process, receiver, "owner", far())
    waiting = enqueue(mem_db)

    supervisor.tick()

    assert "deleted-execution" not in supervisor.children
    assert process.closed and receiver.closed
    assert list(supervisor.children) == [waiting]
    assert reload(mem_db, waiting).status == "RUNNING"


def test_live_child_of_missing_execution_is_stopped(mem_db, supervisor):
    process = FakeProcess(alive=True)
    supervisor.children["deleted-execution"] = executions.Child(process, FakePipeEnd(), "owner", far())
    enqueue(mem_db)

    supervisor.tick()

    assert process.signals == ["terminate"] and process.closed
    assert "deleted-execution" not in supervisor.children


def test_child_deadline_is_armed_before_first_database_read(monkeypatch, child_process):
    # 复核 P1：首次数据库读取阻塞或失败时，子进程也必须已经有独立的硬期限。
    armed_at_first_read: list[list[float]] = []

    def unavailable():
        armed_at_first_read.append([timer.seconds for timer in child_process if timer.started])
        raise OperationalError("SELECT 1", {}, ConnectionError("database unavailable"))

    finished: list[tuple] = []
    monkeypatch.setattr(executions, "SessionLocal", unavailable)
    monkeypatch.setattr(executions, "_finish_owned", lambda *args: finished.append(args))
    sender = FakePipeEnd()

    executions._run_child("execution", "owner", sender, utcnow() + timedelta(seconds=40))

    assert len(armed_at_first_read) == 1 and len(armed_at_first_read[0]) == 1
    assert 30 < armed_at_first_read[0][0] <= 40
    assert finished[0][:3] == ("execution", "owner", "FAILED") and sender.closed
    assert all(timer.cancelled for timer in child_process)


def test_database_deadline_can_only_tighten_the_child_deadline(timers):
    deadline = executions._HardDeadline(60)
    assert 59 < deadline.tighten(600) <= 60 and len(timers) == 1

    assert 9 < deadline.tighten(10) <= 10
    assert [timer.cancelled for timer in timers] == [True, False] and timers[1].started
    assert 9 < timers[1].seconds <= 10

    assert deadline.tighten(-5) == 0 and timers[2].seconds == 0
    deadline.cancel()
    assert all(timer.cancelled for timer in timers)


def test_claim_hands_the_claimed_deadline_to_child_and_supervisor(mem_db, supervisor):
    execution_id, child = running(mem_db, supervisor, timeout_sec=60)
    row = reload(mem_db, execution_id)

    target, args, name, process = supervisor.context.started[0]
    assert target is executions._run_child and name == f"inspection-{execution_id[:8]}"
    assert args[:2] == (execution_id, row.owner_token) and args[3] == row.deadline_at
    assert process.is_alive() and child.token == row.owner_token
    remaining = child.hard_deadline - time.monotonic()
    assert 60 - 5 < remaining <= 60 + executions.HARD_DEADLINE_GRACE


def test_hard_deadline_stops_child_while_database_is_unavailable(mem_db, supervisor, monkeypatch):
    # 复核 P1（监督器侧）：数据库查询失败时，超过期限的子进程仍然必须被停止。
    overdue, healthy = FakeProcess(alive=True), FakeProcess(alive=True)
    supervisor.children["overdue"] = executions.Child(overdue, FakePipeEnd(), "owner", time.monotonic() - 1)
    supervisor.children["healthy"] = executions.Child(healthy, FakePipeEnd(), "owner", far())

    def unavailable():
        raise OperationalError("SELECT 1", {}, ConnectionError("database unavailable"))

    monkeypatch.setattr(executions, "SessionLocal", unavailable)
    with pytest.raises(OperationalError):
        supervisor.tick()

    assert overdue.signals == ["terminate"] and supervisor.children["overdue"].expired
    assert healthy.signals == [] and not supervisor.children["healthy"].expired
    with pytest.raises(OperationalError):
        supervisor.tick()
    assert overdue.signals == ["terminate"], "已停止的子进程不重复发信号"


def test_child_stopped_by_hard_deadline_is_recorded_as_timeout(mem_db, supervisor):
    execution_id, child = running(mem_db, supervisor)
    child.hard_deadline = time.monotonic() - 1

    supervisor.tick()

    row = reload(mem_db, execution_id)
    assert (row.status, row.error_code) == ("TIMEOUT", "LOCAL.DEADLINE")
    assert mem_db.get(InspectionRun, row.run_id).status == "TIMEOUT"
    assert child.process.signals == ["terminate"] and execution_id not in supervisor.children


def locked_sessions(db, locked_id):
    """模拟 PostgreSQL 行锁：FOR UPDATE SKIP LOCKED 跳过被其他事务锁住的记录，普通查询仍能读到它。"""

    class LockedRowSession(Session):
        def scalar(self, statement, *args, **kwargs):
            compiled = statement.compile(dialect=postgresql.dialect())
            if "SKIP LOCKED" in str(compiled) and locked_id in compiled.params.values():
                return None
            return super().scalar(statement, *args, **kwargs)

    return sessionmaker(bind=db.get_bind(), class_=LockedRowSession, autoflush=False, expire_on_commit=False)


def test_locked_row_is_not_mistaken_for_missing_and_exited_child_frees_slot(mem_db, supervisor, monkeypatch):
    execution_id, child = running(mem_db, supervisor)
    waiting = enqueue(mem_db, "ecs_status")  # 同一范围的再次入队会被准入去重，故用另一个任务占位。
    assert waiting != execution_id
    monkeypatch.setattr(executions, "SessionLocal", locked_sessions(mem_db, execution_id))

    supervisor.tick()  # 存活的子进程正在提交结果：遵守提交栅栏，不打断。
    assert child.process.signals == [] and child.locked_since is None

    child.process.alive, child.process.exitcode = False, 1
    supervisor.tick()  # 刚退出：先等一小段时间，争取写入准确的终态。
    assert execution_id in supervisor.children and child.locked_since is not None

    child.locked_since -= executions.LOCKED_EXIT_WAIT
    supervisor.tick()
    assert execution_id not in supervisor.children and child.process.closed
    assert reload(mem_db, execution_id).status == "RUNNING", "记录仍被锁住，终态留给后续 reap"
    assert list(supervisor.children) == [waiting]


def test_truncated_progress_message_from_stopped_child_does_not_abort_the_tick(mem_db, supervisor):
    execution_id, child = running(mem_db, supervisor)

    class TruncatedPipe(FakePipeEnd):
        def poll(self):
            return True

        def recv(self):
            raise OSError("got end of file during message")

    child.receiver = TruncatedPipe()
    child.process.alive, child.process.exitcode = False, -15

    supervisor.tick()

    row = reload(mem_db, execution_id)
    assert (row.status, row.error_code) == ("INTERRUPTED", "LOCAL.PROCESS_EXIT")
    assert execution_id not in supervisor.children


def test_running_child_progress_and_heartbeat_are_persisted(mem_db, supervisor):
    execution_id, child = running(mem_db, supervisor)
    before = reload(mem_db, execution_id)
    lease, history = before.lease_expires_at, len(before.history)
    child.receiver.events.extend([
        {"phase": "collecting", "message": "开始采集"},
        {"phase": "saving", "current": 3, "total": 3, "unit": "resource", "message": "保存巡检结果"}])

    supervisor.tick()

    row = reload(mem_db, execution_id)
    assert row.status == "RUNNING" and row.progress["phase"] == "saving"
    assert [event["phase"] for event in row.history[history:]] == ["collecting", "saving"]
    assert row.heartbeat_at is not None and row.lease_expires_at >= lease
    assert row.lease_expires_at <= row.deadline_at
    assert execution_id in supervisor.children and child.process.signals == []


def test_cancel_request_stops_child_and_records_cancelled(mem_db, supervisor):
    execution_id, child = running(mem_db, supervisor)
    queue.request_cancel(mem_db, execution_id, "operator")

    supervisor.tick()

    row = reload(mem_db, execution_id)
    assert (row.status, row.error_code) == ("CANCELLED", "LOCAL.CANCELLED")
    assert child.process.signals == ["terminate"] and execution_id not in supervisor.children


@pytest.mark.parametrize(("change", "expected"), [
    ({"deadline_at": -1}, ("TIMEOUT", "LOCAL.DEADLINE")),
    ({"exitcode": 124}, ("TIMEOUT", "LOCAL.DEADLINE")),
    ({"lease_expires_at": -1}, ("INTERRUPTED", "LOCAL.LEASE_LOST")),
    ({"exitcode": 1}, ("INTERRUPTED", "LOCAL.PROCESS_EXIT")),
])
def test_stop_reasons_are_recorded_with_distinct_codes(mem_db, supervisor, change, expected):
    execution_id, child = running(mem_db, supervisor)
    row = reload(mem_db, execution_id)
    for column in ("deadline_at", "lease_expires_at"):
        if column in change:
            setattr(row, column, utcnow() + timedelta(seconds=change[column]))
    mem_db.commit()
    if "exitcode" in change:
        child.process.alive, child.process.exitcode = False, change["exitcode"]

    supervisor.tick()

    row = reload(mem_db, execution_id)
    assert (row.status, row.error_code) == expected and row.active_scope is None
    assert mem_db.get(InspectionRun, row.run_id).status == expected[0]
    assert execution_id not in supervisor.children and child.process.closed


def test_terminal_state_committed_by_child_gets_a_short_grace_before_release(mem_db, supervisor):
    execution_id, child = running(mem_db, supervisor)
    queue.finish(mem_db, reload(mem_db, execution_id), "SUCCESS", "完成")
    mem_db.commit()
    child.receiver.events.append({"phase": "saving", "message": "保存巡检结果"})

    supervisor.tick()  # 终态已提交、子进程还在收尾：先不打断。

    row = reload(mem_db, execution_id)
    assert row.status == "SUCCESS" and row.progress["phase"] == "finished" and row.progress["message"] == "完成"
    assert [event["phase"] for event in row.history[-2:]] == ["saving", "finished"]
    assert execution_id in supervisor.children and child.process.signals == []

    child.terminal_seen -= 3
    supervisor.tick()
    assert execution_id not in supervisor.children and child.process.signals == ["terminate"]
    assert reload(mem_db, execution_id).status == "SUCCESS"


def test_child_ignoring_terminate_is_killed(mem_db, supervisor):
    execution_id, child = running(mem_db, supervisor)

    class StubbornProcess(FakeProcess):
        def terminate(self):
            self.signals.append("terminate")

    child.process = StubbornProcess(alive=True)
    queue.request_cancel(mem_db, execution_id, "operator")

    supervisor.tick()

    assert child.process.signals == ["terminate", "kill"] and child.process.closed
    assert reload(mem_db, execution_id).status == "CANCELLED"


def test_spawn_failure_fails_the_execution_and_leaves_the_slot_free(mem_db, supervisor):
    supervisor.context = FakeContext(start_error=OSError("cannot start process"))
    execution_id = enqueue(mem_db)

    with pytest.raises(OSError, match="cannot start process"):
        supervisor.tick()

    row = reload(mem_db, execution_id)
    assert (row.status, row.error_code) == ("FAILED", "LOCAL.SPAWN_FAILED")
    assert mem_db.get(InspectionRun, row.run_id).status == "FAILED"
    assert supervisor.children == {}


def test_shutdown_stops_children_and_records_interrupted(mem_db, supervisor):
    execution_id, child = running(mem_db, supervisor)

    supervisor.close()
    supervisor._loop()  # 停止标志已置位：不再 tick，只执行收尾。

    row = reload(mem_db, execution_id)
    assert (row.status, row.error_code) == ("INTERRUPTED", "LOCAL.SHUTDOWN")
    assert child.process.signals == ["terminate"] and child.receiver.closed
    assert supervisor.children == {}


@pytest.mark.pg
def test_postgres_row_lock_is_told_apart_from_a_missing_record(mem_db, supervisor):
    # 真实行锁：另一个事务持有与提交栅栏相同的行锁时，SKIP LOCKED 不返回记录，但记录仍然存在。
    execution_id, child = running(mem_db, supervisor)
    waiting = enqueue(mem_db, "ecs_status")
    holder = sessionmaker(bind=mem_db.get_bind())()
    try:
        holder.scalar(select(TaskExecution.id).where(TaskExecution.id == execution_id).with_for_update(key_share=True))

        supervisor.tick()
        assert child.process.signals == [] and child.locked_since is None, "存活的子进程不被打断"

        child.process.alive, child.process.exitcode = False, 1
        supervisor.tick()
        assert execution_id in supervisor.children and child.locked_since is not None

        child.locked_since -= executions.LOCKED_EXIT_WAIT
        supervisor.tick()
        assert list(supervisor.children) == [waiting], "已退出的子进程让出槽位，排队的执行被领取"
        assert reload(mem_db, execution_id).status == "RUNNING"
        mem_db.rollback()
    finally:
        holder.rollback()
        holder.close()

    # 锁释放后，租约到期的记录由 reap 写入终态。
    row = reload(mem_db, execution_id)
    row.lease_expires_at = utcnow() - timedelta(seconds=1)
    mem_db.commit()
    supervisor.tick()
    row = reload(mem_db, execution_id)
    assert (row.status, row.error_code) == ("INTERRUPTED", "LOCAL.LEASE_LOST")
    mem_db.rollback()


@pytest.mark.parametrize(("startup_offset", "limit"), [(600, 60), (-50, 10)])
def test_child_runs_under_the_tighter_of_startup_and_database_deadline(
        mem_db, supervisor, child_process, monkeypatch, startup_offset, limit):
    execution_id, _child = running(mem_db, supervisor, timeout_sec=60)
    row = reload(mem_db, execution_id)
    mem_db.get(InspectionTask, "dcs_status").enabled = False  # 建立期限后立即以 SKIPPED 结束，不触发采集。
    mem_db.commit()
    budgets: list[float] = []
    budget = executions.request_budget
    monkeypatch.setattr(executions, "request_budget",
                        lambda **kwargs: budgets.append(kwargs["seconds"]) or budget(**kwargs))

    executions._run_child(execution_id, row.owner_token, FakePipeEnd(),
                          row.deadline_at + timedelta(seconds=startup_offset))

    row = reload(mem_db, execution_id)
    assert (row.status, row.error_code) == ("SKIPPED", "LOCAL.SKIPPED")
    assert len(budgets) == 1 and limit - 10 < budgets[0] <= limit
    assert child_process[-1].seconds <= limit and all(timer.cancelled for timer in child_process)


def test_loop_keeps_ticking_after_a_failed_tick_until_closed(mem_db, supervisor, monkeypatch):
    ticks: list[int] = []

    def tick():
        ticks.append(len(ticks))
        if len(ticks) == 1:
            raise OperationalError("SELECT 1", {}, ConnectionError("database unavailable"))
        supervisor.stopped.set()

    monkeypatch.setattr(supervisor, "tick", tick)
    supervisor.start()
    supervisor.thread.join(timeout=10)
    supervisor.close()

    assert ticks == [0, 1] and not supervisor.thread.is_alive()


def test_child_does_nothing_for_an_execution_it_does_not_own(mem_db, supervisor, child_process, monkeypatch):
    execution_id, _child = running(mem_db, supervisor)
    row = reload(mem_db, execution_id)
    budgets: list[dict] = []
    monkeypatch.setattr(executions, "request_budget", lambda **kwargs: budgets.append(kwargs))
    sender = FakePipeEnd()

    executions._run_child(execution_id, "another-owner", sender, row.deadline_at)

    assert budgets == [] and sender.closed and all(timer.cancelled for timer in child_process)
    assert reload(mem_db, execution_id).status == "RUNNING"


def test_shutdown_releases_children_even_when_the_state_cannot_be_recorded(mem_db, supervisor, monkeypatch):
    execution_id, child = running(mem_db, supervisor)

    def unavailable(*_args):
        raise OperationalError("SELECT 1", {}, ConnectionError("database unavailable"))

    monkeypatch.setattr(executions, "_finish_owned", unavailable)
    supervisor.close()
    supervisor._loop()

    assert child.process.signals == ["terminate"] and child.process.closed
    assert supervisor.children == {}
    assert reload(mem_db, execution_id).status == "RUNNING", "终态留给租约到期后的 reap"
