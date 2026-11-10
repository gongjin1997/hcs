"""隔离进程故障演练入口，由 test_execution_process 调用；仅连接本机合成 HTTP。"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy import func, select

from app.core.crypto import encrypt
from app.core.db import Base, SessionLocal, engine
from app.models import HcsAccount, HcsProject, InspectionRun, InspectionTask, Resource, TaskExecution
from app.services import task_execution as queue
from app.workers.executions import Supervisor


def supervise():
    worker = Supervisor(concurrency=1)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    worker.start()
    try:
        stopped.wait()
    finally:
        worker.close()


def verify():
    state = {"delay": .01, "calls": 0, "writes": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):  # noqa: N802
            state["writes"] += 1
            self.send_error(405)

        def do_GET(self):  # noqa: N802
            state["calls"] += 1
            time.sleep(state["delay"])
            if "instances-logical-nodes" in self.path:
                body = {"instances": [{"instance_id": "cache-1", "nodes": []}], "count": 1}
            elif "statistic" in self.path:
                body = {"statistics": []}
            else:
                body = {"instances": [{"instance_id": "cache-1", "name": "隔离测试缓存", "status": "RUNNING",
                                       "engine": "Redis", "engine_version": "6.0"}], "instance_num": 1}
            payload = json.dumps(body).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{http.server_port}"
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        account = HcsAccount(code="process-test", name="隔离进程测试", iam_endpoint=url, domain_name="test",
                             auth_type="AKSK", access_key="synthetic-key", secret_cipher=encrypt("synthetic-secret"),
                             endpoints={"dcs": url}, enabled=True)
        db.add(account)
        db.flush()
        db.add(HcsProject(account_id=account.id, project_id="test-project", region_id="test-region",
                          external_domain="invalid", enabled=True))
        db.add(InspectionTask(code="dcs_status", name="隔离缓存巡检", service="dcs", enabled=True,
                              interval_sec=3600, params={"timeout_sec": 15}))
        db.commit()

    def launch():
        return subprocess.Popen([sys.executable, __file__, "supervise"], env=os.environ.copy())

    def submit():
        with SessionLocal() as db:
            return queue.enqueue(db, "dcs_status")["execution_ids"][0]

    def wait_for(predicate, timeout=20):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.15)
        raise AssertionError("process condition did not converge")

    def snapshot(eid):
        with SessionLocal() as db:
            row = db.get(TaskExecution, eid)
            run = db.get(InspectionRun, row.run_id)
            assert row.status == run.status
            return {"status": row.status, "finished_at": row.finished_at, "heartbeat": row.heartbeat_at,
                    "api_calls": run.api_calls, "progress": row.progress, "lease": row.lease_expires_at}

    results = {}
    service = launch()
    try:
        success = submit()
        wait_for(lambda: snapshot(success)["status"] not in queue.ACTIVE)
        assert snapshot(success)["status"] == "SUCCESS", snapshot(success)
        assert snapshot(success)["api_calls"] == 3
        results["success"] = "same persisted run, 3 read-only cloud requests"

        # 三次各5.5秒均小于单请求10秒超时，整轮仍须在15秒结束。
        state["delay"] = 5.5
        before = state["calls"]
        timeout = submit()
        started = time.monotonic()
        wait_for(lambda: snapshot(timeout)["status"] not in queue.ACTIVE, timeout=22)
        assert snapshot(timeout)["status"] == "TIMEOUT", snapshot(timeout)
        assert time.monotonic() - started < 20 and state["calls"] >= before + 2
        results["deadline"] = "whole-run deadline stops individually valid requests"

        before = state["calls"]
        cancelled = submit()
        wait_for(lambda: state["calls"] > before)
        with SessionLocal() as db:
            queue.request_cancel(db, cancelled, "local-test")
        wait_for(lambda: snapshot(cancelled)["status"] == "CANCELLED", timeout=5)
        count = state["calls"]
        time.sleep(1)
        assert state["calls"] == count and state["writes"] == 0
        results["cancel"] = "local child stopped, no cloud mutation requests"

        # SIGKILL 整个监督器，重启后活租约不被当成已失联；随后租约按真实时钟过期。
        with SessionLocal() as db:
            db.get(InspectionTask, "dcs_status").params = {"timeout_sec": 90}
            db.commit()
        state["delay"] = 9
        before = state["calls"]
        interrupted = submit()
        wait_for(lambda: state["calls"] > before)
        service.kill()
        service.wait(timeout=5)
        with SessionLocal() as db:
            queue.reap(db)
        assert snapshot(interrupted)["status"] == "RUNNING"
        service = launch()
        time.sleep(1)
        assert snapshot(interrupted)["status"] == "RUNNING"
        wait_for(lambda: snapshot(interrupted)["status"] == "INTERRUPTED", timeout=40)
        state["delay"] = .01
        retry = submit()
        assert retry != interrupted
        wait_for(lambda: snapshot(retry)["status"] not in queue.ACTIVE)
        assert snapshot(retry)["status"] == "SUCCESS"
        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(InspectionRun)) == 5
            assert db.scalar(select(func.count()).select_from(Resource)) == 1
        assert snapshot(interrupted)["status"] == "INTERRUPTED" and state["writes"] == 0
        results["restart"] = "SIGKILL + restart preserves live lease, reclaims orphan, retry creates new history"
        print("PROCESS_PROBE=" + json.dumps(results, ensure_ascii=False), flush=True)
    finally:
        if service.poll() is None:
            service.terminate()
            service.wait(timeout=15)
        http.shutdown()
        http.server_close()


if __name__ == "__main__":
    supervise() if len(sys.argv) > 1 and sys.argv[1] == "supervise" else verify()
