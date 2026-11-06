"""局域网启动前检查的回归测试，仅使用随机回环端口。"""
import os
import json
import io
import signal
import time
from contextlib import redirect_stderr
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import lan
from lan import RotatingOutput, check_port, live, start_time


class StartupTests(unittest.TestCase):
    def test_listening_port_is_rejected(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with self.assertRaises(RuntimeError):
                check_port("127.0.0.1", listener.getsockname()[1])

    def test_recently_closed_http_port_can_be_reused(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with socket.create_connection(("127.0.0.1", port)) as client:
                connection, _ = listener.accept()
                with connection:
                    connection.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b"")
        check_port("127.0.0.1", port)

    def test_dead_or_reused_pid_is_not_reported_as_running(self):
        self.assertFalse(live({"pid": -1, "started": None}))
        self.assertFalse(live({"pid": os.getpid(), "started": "wrong-start-time"}))
        self.assertTrue(live({"pid": os.getpid(), "started": start_time(os.getpid())}))


class LogTests(unittest.TestCase):
    def test_rotated_files_are_bounded_and_private(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "logs"
            writer = RotatingOutput(directory, "api", max_bytes=256, backups=2)
            try:
                for index in range(50):
                    writer.write({"service": "api", "index": index, "msg": "x" * 80})
                writer.write({"msg": "x" * 2000})
            finally:
                writer.close()
            files = list(directory.iterdir())
            self.assertLessEqual(len(files), 3)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for file in files:
                self.assertLessEqual(file.stat().st_size, 256)
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
                for line in file.read_text().splitlines():
                    self.assertIsInstance(json.loads(line), dict)

    def test_writer_refuses_symlinks_without_touching_target(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "logs"
            directory.mkdir(mode=0o700)
            target = Path(folder) / "untouched"
            target.write_text("keep")
            (directory / "api.jsonl").symlink_to(target)
            with self.assertRaises(OSError):
                RotatingOutput(directory, "api", max_bytes=256)
            self.assertEqual(target.read_text(), "keep")

    def test_capture_preserves_early_stderr_and_return_code_with_size_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "logs"
            command = [sys.executable, "-c", "import sys; print('early import failure',file=sys.stderr); sys.exit(7)"]
            self.assertEqual(lan.capture(command, directory, "mock", max_bytes=512, backups=1), 7)
            # 生命周期元数据可触发轮转；早期 stderr 仍须保留在当前或备份文件中。
            self.assertIn("early import failure", "".join(p.read_text() for p in directory.glob("mock.jsonl*")))
            last = json.loads((directory / "mock.jsonl").read_text().splitlines()[-1])
            self.assertEqual((last["event"], last["level"], last["exit_code"]), ("process.exited", "ERROR", 7))

    def test_supervisor_drains_graceful_shutdown_output(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "logs"
            script = "import signal,time,json,sys\ndef stop(*args):\n print(json.dumps({'event':'shutdown.complete'}),flush=True)\n sys.exit(0)\nsignal.signal(signal.SIGTERM,stop)\nprint(json.dumps({'event':'ready'}),flush=True)\ntime.sleep(30)"
            process = subprocess.Popen([sys.executable, str(Path(lan.__file__)), "_stream", "--service", "mock",
                                        "--log-dir", str(directory), "--", sys.executable, "-c", script],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
            try:
                path = directory / "mock.jsonl"
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if path.exists() and '"event": "ready"' in path.read_text():
                        break
                    time.sleep(0.02)
                else:
                    self.fail("isolated supervisor did not become ready")
                os.killpg(process.pid, signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertIn("shutdown.complete", path.read_text())
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                process.stderr.close()

    def test_log_io_failure_does_not_kill_business_and_reports_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            original = RotatingOutput.write
            calls = 0

            def flaky(writer, record):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("synthetic disk failure")
                original(writer, record)

            directory = Path(folder) / "logs"
            script = "import time; print('first',flush=True); time.sleep(1.1); print('second',flush=True)"
            stderr = io.StringIO()
            with patch.object(RotatingOutput, "write", flaky), redirect_stderr(stderr):
                code = lan.capture([sys.executable, "-c", script], directory, "mock")
            self.assertEqual(code, 0)
            self.assertIn("log.write_failed", stderr.getvalue())
            records = [json.loads(line) for line in (directory / "mock.jsonl").read_text().splitlines()]
            self.assertEqual(records[0]["event"], "log.write_recovered")
            self.assertGreaterEqual(records[0]["dropped_records"], 1)
            self.assertTrue(any(record.get("msg") == "second" for record in records))
            self.assertEqual(records[-1]["exit_code"], 0)

    def test_planned_sigterm_is_not_a_crash_and_keeps_raw_signal(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "logs"
            child = "import time; print('ready',flush=True); time.sleep(30)"
            process = subprocess.Popen([sys.executable, str(Path(lan.__file__)), "_stream", "--service", "mock",
                                        "--log-dir", str(directory), "--", sys.executable, "-c", child],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
            try:
                path = directory / "mock.jsonl"
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if path.exists() and '"msg": "ready"' in path.read_text():
                        break
                    time.sleep(0.02)
                else:
                    self.fail("isolated supervisor not ready")
                os.killpg(process.pid, signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 0)
                last = json.loads(path.read_text().splitlines()[-1])
                self.assertEqual((last["level"], last["exit_code"], last["exit_signal"]), ("INFO", -signal.SIGTERM, "SIGTERM"))
                self.assertTrue(last["expected_shutdown"])
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                process.stderr.close()

    def test_stream_environment_disables_double_write_and_env_file(self):
        env = lan.stream_environment({"INSPECTOR_LOG_DIR": "/ignored", "INSPECTOR_ENV_FILE": "/ignored", "PATH": "/test"})
        self.assertNotIn("INSPECTOR_LOG_DIR", env)
        self.assertEqual(env["INSPECTOR_ENV_FILE"], "")
        self.assertEqual(env["PATH"], "/test")

    def test_spawn_uses_supervisor_and_shared_process_group(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(lan, "STATE", Path(folder)), \
                patch.object(lan, "processes", return_value={}), patch.object(lan, "start_time", return_value="test"), \
                patch.object(lan.subprocess, "Popen") as popen:
            popen.return_value.pid = 123
            lan.spawn("api", ["python", "-m", "uvicorn"], {"INSPECTOR_LOG_DIR": "/ignored"}, Path(folder))
            command = popen.call_args.args[0]
            self.assertIn("_stream", command)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertNotIn("INSPECTOR_LOG_DIR", popen.call_args.kwargs["env"])

    def test_diagnostics_never_loads_runtime_config_or_runs_lan_lifecycle(self):
        with patch.object(lan, "load_config", side_effect=AssertionError("no config read")), \
                patch.object(lan, "start", side_effect=AssertionError("no start")), \
                patch.object(lan, "stop", side_effect=AssertionError("no stop")), \
                patch.object(lan.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(lan.main(["diagnostics"]), 0)
            command = run.call_args.args[0]
            self.assertIn("app.tools.diagnostics", command)
            self.assertEqual(run.call_args.kwargs["env"]["INSPECTOR_ENV_FILE"], "")
