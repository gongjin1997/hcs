#!/usr/bin/env python3
"""独立局域网演示：只使用自身容器、数据库、进程和随机密钥。"""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".runtime" / "lan"
PYTHON = ROOT / "backend" / ".venv" / "bin" / "python"
CONTAINERS = ("hcs-inspector-lan-postgres", "hcs-inspector-lan-redis", "hcs-inspector-lan-mailpit")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
LOGS = STATE / "logs"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 5
LOG_SERVICES = {"api", "scheduler", "notifier", "mock", "web", "startup", "migrate", "bootstrap", "build", "containers", "stop"}


def _log_helpers():
    sys.path.insert(0, str(ROOT / "backend"))
    from app.tools.diagnostics import private_directory
    return private_directory


class RotatingOutput:
    """单监督进程单文件；固定字节上限，不依赖后台服务重新打开 stdout。"""

    def __init__(self, directory, name, max_bytes=LOG_MAX_BYTES, backups=LOG_BACKUPS):
        if name not in LOG_SERVICES or max_bytes < 128 or not 0 <= backups <= 20:
            raise ValueError("日志服务名或轮转限制不正确")
        self.directory = _log_helpers()(directory)
        self.name, self.max_bytes, self.backups = f"{name}.jsonl", max_bytes, backups
        self.fd = None
        try:
            self._open()
        except Exception:
            os.close(self.directory)
            raise

    def _open(self):
        fd = os.open(self.name, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     0o600, dir_fd=self.directory)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            os.close(fd)
            raise ValueError("日志文件必须是当前用户独立的常规文件")
        os.fchmod(fd, 0o600)
        self.fd = fd

    def write(self, record):
        if self.fd is None:
            self._open()
        data = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(data) > self.max_bytes:
            data = b'{"event":"oversized_log_record_omitted"}\n'
        if os.fstat(self.fd).st_size + len(data) > self.max_bytes:
            if self.backups:
                os.close(self.fd)
                self.fd = None
                for index in range(self.backups, 0, -1):
                    old = self.name if index == 1 else f"{self.name}.{index - 1}"
                    new = f"{self.name}.{index}"
                    try:
                        info = os.stat(old, dir_fd=self.directory, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError("轮转日志包含非独立常规文件")
                    os.rename(old, new, src_dir_fd=self.directory, dst_dir_fd=self.directory)
                self._open()
            else:
                os.ftruncate(self.fd, 0)
        view = memoryview(data)
        while view:
            written = os.write(self.fd, view)
            view = view[written:]

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        os.close(self.directory)


def capture(command, directory, name, *, max_bytes=LOG_MAX_BYTES, backups=LOG_BACKUPS):
    """监督器用子进程同一进程组运行目标，保存含启动早期错误的有界输出。"""
    sys.path.insert(0, str(ROOT / "backend"))
    from app.core.logging import sanitize_log, sanitize_text

    writer = RotatingOutput(directory, name, max_bytes, backups)
    process = None
    old_handler = None
    retry_at, dropped = 0.0, 0
    stop_requested = False

    def terminate(_signum, _frame):
        nonlocal stop_requested
        # LAN stop 对共享进程组发信号，目标已收到 SIGTERM；重复转发会打断其退出处理。
        # 监督器继续排空输出，目标退出后随之结束；强制清理仍由 stop 的组级 SIGKILL 保证。
        stop_requested = True

    def persist(record):
        nonlocal retry_at, dropped
        now = time.monotonic()
        if now < retry_at:
            dropped += 1
            return
        try:
            if dropped:
                writer.write({"service": name, "level": "WARNING", "event": "log.write_recovered",
                              "dropped_records": dropped})
            writer.write(record)
            dropped = 0
        except OSError:
            dropped += 1
            retry_at = now + 1
            # 磁盘故障不能终止业务，也不回显原始 record 或系统路径。
            sys.stderr.write('{"level":"ERROR","event":"log.write_failed"}\n')

    if threading.current_thread() is threading.main_thread():
        old_handler = signal.signal(signal.SIGTERM, terminate)
    try:
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as error:
            persist({"service": name, "level": "ERROR", "event": "launch_failed", "error_type": type(error).__name__})
            return 1
        persist({"ts": datetime.now(UTC).isoformat(), "service": name, "logger": "lan.supervisor", "level": "INFO",
                 "event": "process.started", "supervisor_pid": os.getpid(), "child_pid": process.pid})
        maximum = min(64 * 1024 + 1, max_bytes // 4)
        with process.stdout as output:
            while raw := output.readline(maximum + 1):
                if len(raw) > maximum:
                    persist({"service": name, "event": "oversized_output_line_omitted"})
                    while not raw.endswith(b"\n") and raw:
                        raw = output.readline(maximum + 1)
                    continue
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("not object")
                except (ValueError, RecursionError):
                    record = {"ts": datetime.now(UTC).isoformat(), "service": name, "logger": "lan.stdout",
                              "level": "INFO", "msg": sanitize_text(line)}
                persist(sanitize_log(record))
        result = process.wait()
        expected_stop = stop_requested and result in (0, -signal.SIGTERM)
        persist({"ts": datetime.now(UTC).isoformat(), "service": name, "logger": "lan.supervisor",
                 "level": "INFO" if result == 0 or expected_stop else "ERROR", "event": "process.exited",
                 "exit_code": result, "expected_shutdown": expected_stop,
                 "exit_signal": "SIGTERM" if result == -signal.SIGTERM else None})
        return 0 if expected_stop else result
    finally:
        if old_handler is not None:
            signal.signal(signal.SIGTERM, old_handler)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        writer.close()


def stream_command(name, command):
    return [str(PYTHON), str(ROOT / "scripts" / "lan.py"), "_stream", "--service", name,
            "--log-dir", str(LOGS), "--", *command]


def stream_environment(env=None):
    value = dict(os.environ if env is None else env)
    value.pop("INSPECTOR_LOG_DIR", None)
    value["INSPECTOR_ENV_FILE"] = ""
    return value


def run(command, *, env=None, cwd=ROOT, log="startup"):
    result = subprocess.run(stream_command(log, command), env=stream_environment(env), cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError(f"步骤 {log} 失败，请查看 {LOGS / (log + '.jsonl')}")


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    path.chmod(0o600)


def lan_host():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("192.168.2.1", 9))
        return sock.getsockname()[0]


def start_time(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, IndexError):
        return None


def live(record):
    if not record:
        return False
    actual = start_time(record["pid"])
    return actual is not None and actual == record["started"]


def load_config(host=None, port=None):
    path = STATE / "config.json"
    if path.exists():
        config = json.loads(path.read_text())
    else:
        config = {"host": host or lan_host(), "port": port or 5173,
                  "pg_password": secrets.token_hex(24), "jwt_secret": secrets.token_hex(32),
                  "mail_secret": secrets.token_hex(24), "master_key": base64.b64encode(secrets.token_bytes(32)).decode()}
    if host:
        config["host"] = host
    if port:
        config["port"] = port
    save(path, config)
    return config


def environment(config):
    url = f"http://{config['host']}:{config['port']}"
    # LAN 由监督器唯一落盘；移除继承的原生日志目录，避免 JSON stdout 和原生文件双写。
    inherited = {key: value for key, value in os.environ.items() if key != "INSPECTOR_LOG_DIR"}
    return {**inherited, "INSPECTOR_ENV_FILE": "", "INSPECTOR_ENV": "dev", "INSPECTOR_ENV_TAG": "LAN-DEMO",
            "INSPECTOR_DATABASE_URL": f"postgresql+psycopg://hcs_lan:{config['pg_password']}@127.0.0.1:55433/hcs_lan",
            "INSPECTOR_REDIS_URL": "redis://127.0.0.1:16379/0", "INSPECTOR_DEMO_SEED": "true",
            "INSPECTOR_INIT_ADMIN_PASSWORD": "", "INSPECTOR_JWT_SECRET": config["jwt_secret"],
            "INSPECTOR_MAIL_LINK_SECRET": config["mail_secret"], "INSPECTOR_MASTER_KEY": config["master_key"],
            "INSPECTOR_BASE_URL": url, "INSPECTOR_CORS_ORIGINS": json.dumps([url]),
            "INSPECTOR_HCS_MOCK_URL": "http://127.0.0.1:19000", "INSPECTOR_SMTP_HOST": "127.0.0.1",
            "INSPECTOR_HCS_CES_ENABLED": "false",
            "INSPECTOR_SMTP_PORT": "11025", "INSPECTOR_SMTP_USER": "", "INSPECTOR_SMTP_PASSWORD": "",
            "INSPECTOR_SMTP_STARTTLS": "false", "INSPECTOR_METRICS_TOKEN": "", "PYTHONUNBUFFERED": "1",
            "VITE_API_TARGET": "http://127.0.0.1:18000", "NO_PROXY": f"127.0.0.1,localhost,{config['host']}"}


def container(name, image, args):
    check = subprocess.run(["docker", "inspect", "-f", '{{index .Config.Labels "hcs-inspector.lan"}}', name],
                           capture_output=True, text=True)
    if check.returncode == 0:
        if check.stdout.strip() != str(ROOT):
            raise RuntimeError(f"同名容器 {name} 不属于本项目，拒绝接管")
        run(["docker", "start", name], log="containers")
    else:
        run(["docker", "run", "-d", "--name", name, "--label", f"hcs-inspector.lan={ROOT}",
             "--restart", "unless-stopped", "--log-driver", "json-file", "--log-opt", "max-size=50m",
             "--log-opt", "max-file=5", *args, image], log="containers")


def wait_http(url, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with OPENER.open(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f"服务未就绪：{url}")


def processes():
    path = STATE / "processes.json"
    return json.loads(path.read_text()) if path.exists() else {}


def spawn(name, command, env, cwd):
    records = processes()
    if live(records.get(name)):
        return
    process = subprocess.Popen(stream_command(name, command), env=stream_environment(env), cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    records[name] = {"pid": process.pid, "started": start_time(process.pid)}
    save(STATE / "processes.json", records)


def check_port(host, port):
    with socket.socket() as sock:
        # 与实际 HTTP 服务一致，允许复用已关闭连接的 TIME_WAIT 端口；仍拒绝正在监听的端口。
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            raise RuntimeError(f"端口已占用或无法绑定：{host}:{port}") from None


def start(config):
    if any(live(p) for p in processes().values()):
        raise RuntimeError("已有局域网进程在运行；请先 make lan-stop 再启动新版本，或 make lan-status 查看")
    for host, port in [(config["host"], config["port"]), ("127.0.0.1", 18000), ("127.0.0.1", 19000)]:
        check_port(host, port)
    env = environment(config)
    container(CONTAINERS[0], "postgres:16-alpine", ["-p", "127.0.0.1:55433:5432", "-e", "POSTGRES_USER=hcs_lan",
        "-e", "POSTGRES_DB=hcs_lan", "-e", f"POSTGRES_PASSWORD={config['pg_password']}",
        "-v", "hcs-inspector-lan-postgres:/var/lib/postgresql/data"])
    container(CONTAINERS[1], "redis:7-alpine", ["-p", "127.0.0.1:16379:6379"])
    container(CONTAINERS[2], "axllent/mailpit:latest", ["-p", "127.0.0.1:11025:1025", "-p", "127.0.0.1:18025:8025"])
    for _ in range(60):
        check = subprocess.run(["docker", "exec", CONTAINERS[0], "pg_isready", "-U", "hcs_lan", "-d", "hcs_lan"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if check.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("PostgreSQL 未就绪")
    wait_http("http://127.0.0.1:18025/")
    run([str(PYTHON), "-m", "alembic", "upgrade", "head"], env=env, cwd=ROOT / "backend", log="migrate")
    run([str(PYTHON), "-m", "app.workers.bootstrap"], env=env, cwd=ROOT / "backend", log="bootstrap")
    spawn("mock", [str(PYTHON), "-m", "uvicorn", "mock_hcs.app:app", "--host", "127.0.0.1", "--port", "19000"],
          env, ROOT / "backend")
    wait_http("http://127.0.0.1:19000/openapi.json")
    spawn("api", [str(PYTHON), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "18000",
                  "--timeout-graceful-shutdown", "8"],
          env, ROOT / "backend")
    wait_http("http://127.0.0.1:18000/readyz")
    spawn("scheduler", [str(PYTHON), "-m", "app.workers.scheduler"], env, ROOT / "backend")
    spawn("notifier", [str(PYTHON), "-m", "app.workers.notifier"], env, ROOT / "backend")
    run(["npm", "run", "build"], env=env, cwd=ROOT / "frontend", log="build")
    spawn("web", ["node", "node_modules/vite/bin/vite.js", "preview", "--host", config["host"],
                  "--port", str(config["port"]), "--strictPort"], env, ROOT / "frontend")
    wait_http(f"http://{config['host']}:{config['port']}/")


def stop():
    records = list(processes().values())
    for record in records:
        if live(record):
            os.killpg(record["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 10
    while any(live(record) for record in records) and time.monotonic() < deadline:
        time.sleep(0.2)
    for record in records:
        if live(record):
            os.killpg(record["pid"], signal.SIGKILL)
    for name in CONTAINERS:
        check = subprocess.run(["docker", "inspect", "-f", '{{index .Config.Labels "hcs-inspector.lan"}}', name],
                               capture_output=True, text=True)
        if check.returncode == 0 and check.stdout.strip() == str(ROOT):
            run(["docker", "stop", name], log="stop")


def status(config):
    records = processes()
    ready = {}
    for name, url in {"web": f"http://{config['host']}:{config['port']}/",
                      "api": "http://127.0.0.1:18000/readyz", "mock": "http://127.0.0.1:19000/openapi.json"}.items():
        try:
            with OPENER.open(url, timeout=2) as response:
                ready[name] = response.status == 200
        except OSError:
            ready[name] = False
    print(json.dumps({"url": f"http://{config['host']}:{config['port']}", "mode": "Mock HCS 局域网演示",
        "ready": ready,
        "processes": {name: {"pid": value["pid"], "running": live(value)} for name, value in records.items()},
        "logs": str(LOGS)}, ensure_ascii=False, indent=2))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "_stream":
        parser = argparse.ArgumentParser(description="内部日志监督器")
        parser.add_argument("--service", choices=sorted(LOG_SERVICES), required=True)
        parser.add_argument("--log-dir", type=Path, required=True)
        parser.add_argument("command", nargs=argparse.REMAINDER)
        args = parser.parse_args(argv[1:])
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("需要目标命令")
        return capture(command, args.log_dir, args.service)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status", "diagnostics"])
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    if args.action == "diagnostics":
        # 诊断不读取 config/processes，不探测 HTTP，不启动/停止任何已有进程。
        return subprocess.run([str(PYTHON), "-m", "app.tools.diagnostics", "bundle", "--log-dir", str(LOGS),
                               "--output-dir", str(STATE / "diagnostics")], cwd=ROOT / "backend",
                               env=stream_environment()).returncode
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.action == "start" and any(live(value) for value in processes().values()):
        raise SystemExit("已有局域网进程在运行；请先 make lan-stop，或 make lan-status 查看")
    config = load_config(args.host, args.port)
    try:
        if args.action == "start":
            start(config)
        elif args.action == "stop":
            stop()
        status(config)
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
