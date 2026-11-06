import asyncio
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from app.api.v1 import diagnostics
from app.core import logging as logs
from app.core.deps import CurrentUser, get_current_user


def test_formatter_keeps_context_extras_and_utc_without_secrets():
    formatter = logs.JsonFormatter("scheduler")
    with logs.log_context(request_id="request_123", tenant_id=12, project_ref=7):
        with logs.log_context(task="dcs_status", run_id=91):
            logs.bind_log_context(user_id=3)
            record = logging.makeLogRecord({
                "msg": "finished", "name": "collector", "levelno": 20, "levelname": "INFO",
                "resource_total": 15, "attempt": 2, "level": "spoofed", "service": "spoofed",
                "detail": {"access_key": "private-ak", "authorization": "Bearer private-jwt",
                           "nested": {"password": "private-pw"}, "count": 4},
            })
            data = json.loads(formatter.format(record))
            assert data["tenant_id"] == 12 and data["project_ref"] == 7
            assert data["task"] == "dcs_status" and data["run_id"] == 91 and data["user_id"] == 3
            assert data["resource_total"] == 15 and data["attempt"] == 2
            assert data["level"] == "INFO" and data["service"] == "scheduler"
            assert data["ts"].endswith("Z") and data["detail"]["count"] == 4
            assert "private-" not in json.dumps(data)
        assert "task" not in json.loads(formatter.format(logging.makeLogRecord({"msg": "outside"})))
    assert "tenant_id" not in json.loads(formatter.format(logging.makeLogRecord({"msg": "outside"})))


@pytest.mark.parametrize("value,secret", [
    ("password='internal password'", "internal password"),
    ('{"smtp_password": "internal password"}', "internal password"),
    ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
    ("https://name:password@internal.invalid/a?token=private-token", "private-token"),
    ("postgresql+psycopg://name:private-pw@local/db", "private-pw"),
    ("redis://:private-pw@local/0", "private-pw"),
    ("/api/v1/mail-actions/private-mail-token", "private-mail-token"),
    ("GET /m/action/private-mail-token?next=private-data", "private-mail-token"),
    ("[parameters: {'password': 'private-sql-value'}]\n(details)", "private-sql-value"),
    ("-----BEGIN PRIVATE KEY-----\nprivate-key-body\n-----END PRIVATE KEY-----", "private-key-body"),
])
def test_text_sanitization(value, secret):
    assert secret not in logs.sanitize_text(value)


def test_sanitization_handles_non_json_and_bounds():
    value = {"values": [float("inf"), {1, 2}], "text": "x" * 100000}
    text = json.dumps(logs.sanitize_log(value), allow_nan=False)
    assert len(text) < 34000
    assert logs.safe_path("/api/v1/mail-actions/secret?ticket=secret") == "/api/v1/mail-actions/[redacted]"
    for invalid in ("short", "bad\nheader", "x" * 65, "含中文请求编号"):
        generated = logs.request_id(invalid)
        assert len(generated) == 32 and generated != invalid
    assert logs.request_id("request_123") == "request_123"


def test_formatter_bounds_one_record_and_keeps_error_location():
    try:
        raise RuntimeError("technical failure")
    except RuntimeError:
        import sys

        record = logging.makeLogRecord({"msg": "x" * 100000, "request_id": "request_123", "run_id": 9,
                                        "exc_info": sys.exc_info(), "large": ["内容" * 20000] * 100})
    formatted = logs.JsonFormatter().format(record)
    assert len(formatted.encode()) <= 64 * 1024
    data = json.loads(formatted)
    assert data["request_id"] == "request_123" and data["run_id"] == 9
    assert "RuntimeError: technical failure" in data["exc"]
    assert "large" not in data and data["truncated_fields"] >= 1


def test_logging_io_failure_does_not_dump_unsanitized_arguments(capsys):
    class Broken:
        def write(self, value):
            raise OSError("disk unavailable")

        def flush(self):
            pass

    handler = logs.SafeStreamHandler(Broken())
    handler.setFormatter(logs.JsonFormatter())
    record = logging.makeLogRecord({"msg": "password=%s", "args": ("private-value",)})
    handler.emit(record)
    error = capsys.readouterr().err
    assert "log.write_failed" in error and "private-value" not in error


def test_file_rotation_is_private_bounded_and_idempotent(tmp_path, monkeypatch):
    settings = SimpleNamespace(log_level="INFO", log_dir=str(tmp_path), log_max_bytes=1024, log_backup_count=2)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    root = logging.getLogger()
    prior, level = list(root.handlers), root.level
    try:
        logs.setup_logging(service="test_logging")
        logs.setup_logging(service="test_logging")
        assert len([h for h in root.handlers if getattr(h, "_inspector", False)]) == 2
        logger = logging.getLogger("rotation.test")
        for i in range(30):
            logger.info("sample %s %s", i, "x" * 120, extra={"password": "private-password"})
        files = list(tmp_path.iterdir())
        assert len(files) == 3
        for path in files:
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.stat().st_size <= 1024
            for line in path.read_text().splitlines():
                assert "private-password" not in line
                assert json.loads(line)["service"] == "test_logging"
    finally:
        for handler in list(root.handlers):
            if handler not in prior:
                root.removeHandler(handler)
                handler.close()
        root.handlers[:] = prior
        root.setLevel(level)


def test_log_file_refuses_symlink(tmp_path, monkeypatch):
    victim = tmp_path / "private"
    victim.write_text("unchanged")
    (tmp_path / "api.jsonl").symlink_to(victim)
    settings = SimpleNamespace(log_level="INFO", log_dir=str(tmp_path), log_max_bytes=1024, log_backup_count=2)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    with pytest.raises(ValueError, match="符号链接"):
        logs.setup_logging(service="api")
    assert victim.read_text() == "unchanged"


@pytest.mark.parametrize("kind", ["hardlink", "fifo"])
def test_native_log_file_refuses_shared_or_special_files(tmp_path, monkeypatch, kind):
    victim = tmp_path / "private"
    victim.write_text("unchanged")
    target = tmp_path / "api.jsonl"
    if kind == "hardlink":
        os.link(victim, target)
    else:
        os.mkfifo(target)
    settings = SimpleNamespace(log_level="INFO", log_dir=str(tmp_path), log_max_bytes=1024, log_backup_count=2)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    with pytest.raises((ValueError, OSError)):
        logs.setup_logging(service="api")
    assert victim.read_text() == "unchanged"


def test_request_log_streams_counts_and_redacts_path(caplog):
    app = FastAPI()
    app.add_middleware(logs.RequestIdMiddleware)

    @app.get("/api/v1/mail-actions/{token}")
    async def stream(token: str):
        async def chunks():
            yield b"first"
            await asyncio.sleep(0)
            yield b"second"
        return StreamingResponse(chunks())

    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        response = client.get("/api/v1/mail-actions/private-token?password=private-query",
                              headers={"X-Request-Id": "request_123"})
    assert response.content == b"firstsecond"
    assert response.headers["X-Request-Id"] == "request_123"
    record = next(r for r in caplog.records if r.name == "access")
    data = json.loads(logs.JsonFormatter().format(record))
    assert record.response_bytes == 11 and record.status == 200
    assert "private-" not in json.dumps(data)


def test_unhandled_error_keeps_request_id_and_single_trace(client, caplog):
    from app.main import app

    async def crash():
        raise RuntimeError("password='private-pw'")

    app.add_api_route("/_logging-review/crash", crash, include_in_schema=False)
    try:
        with TestClient(app, raise_server_exceptions=False) as local, caplog.at_level(logging.ERROR):
            response = local.get("/_logging-review/crash", headers={"X-Request-Id": "request_123"})
        assert response.status_code == 500
        assert response.headers["X-Request-Id"] == "request_123"
        assert response.json()["data"]["request_id"] == "request_123"
        traces = [r for r in caplog.records if r.exc_info and r.getMessage() == "request.failed"]
        assert len(traces) == 1
        assert "private-pw" not in logs.JsonFormatter().format(traces[0])
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/_logging-review/crash"]


def test_concurrent_request_context_does_not_leak(caplog):
    async def run():
        async def inner(scope, receive, send):
            logs.bind_log_context(user_id=scope["user_id"])
            await asyncio.sleep(0)
            logging.getLogger("context.test").info("inside", extra={"captured": dict(logs._context.get())})
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def send(message):
            pass

        async def receive():
            return {"type": "http.request", "body": b""}

        middleware = logs.RequestIdMiddleware(inner)
        await asyncio.gather(*(middleware({"type": "http", "method": "GET", "path": "/test", "user_id": n,
                                          "headers": [(b"x-request-id", f"request_{n}".encode())]}, receive, send)
                               for n in range(10)))
    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    rows = [r.captured for r in caplog.records if r.name == "context.test"]
    assert len(rows) == 10
    assert all(row["request_id"] == f"request_{row['user_id']}" for row in rows)


def test_browser_reports_are_local_authenticated_bounded_and_deduplicated(caplog):
    app = FastAPI()
    app.include_router(diagnostics.router)
    user = CurrentUser(id=9393, username="test", display_name="test")
    app.dependency_overrides[get_current_user] = lambda: user
    diagnostics._windows.clear()
    body = {"kind": "vue", "error_name": "TypeError", "route": "/dcs", "message": "private-business-content",
            "stack": "at render (http://private-host/assets/page.js:20:7?token=private-token)",
            "request_id": "request_123"}
    with TestClient(app) as client, caplog.at_level(logging.ERROR):
        assert client.post("/diagnostics/client-errors", json=body).status_code == 200
        assert client.post("/diagnostics/client-errors", json=body).status_code == 200
    rows = [r for r in caplog.records if r.name == "browser"]
    assert len(rows) == 1 and rows[0].frames == ["page.js:20:7"]
    assert rows[0].client_request_id == "request_123"
    assert "private-" not in logs.JsonFormatter().format(rows[0])
    assert diagnostics._admit(user.id, "another")
    for n in range(8):
        assert diagnostics._admit(user.id, f"unique-{n}")
    assert not diagnostics._admit(user.id, "over-limit")


def test_browser_report_requires_login_and_limits_body(client, admin):
    assert client.post("/api/v1/diagnostics/client-errors", json={"kind": "error"}).status_code == 401
    assert client.post("/api/v1/diagnostics/client-errors", headers=admin, content=b"x" * 8193).status_code == 413
    assert client.post("/api/v1/diagnostics/client-errors", headers=admin, content=b"not-json").status_code == 400
    assert client.post("/api/v1/diagnostics/client-errors", headers=admin,
                       json={"kind": "error", "password": "not-allowed"}).status_code == 400


def test_audit_file_log_waits_for_real_commit_and_omits_business_detail(mem_db, caplog):
    from app.core.audit import Auditor

    auditor = Auditor(mem_db, None, "127.0.0.1")
    with caplog.at_level(logging.INFO, logger="audit"):
        auditor.log("setting.update", target_type="setting", target_id=7, tenant_id=None,
                    detail={"password": "private-password"}, commit=False)
        assert not [r for r in caplog.records if r.name == "audit"]
        mem_db.commit()
    rows = [r for r in caplog.records if r.name == "audit"]
    assert len(rows) == 1 and rows[0].action == "setting.update" and rows[0].target_id == "7"
    assert "private-password" not in logs.JsonFormatter().format(rows[0])
    with caplog.at_level(logging.INFO, logger="audit"):
        auditor.log("must.not.log", commit=False)
        mem_db.rollback()
        mem_db.commit()
    assert len([r for r in caplog.records if r.name == "audit"]) == 1


def test_nested_audit_rollback_keeps_parent_and_savepoint_commit_waits(mem_db, caplog):
    from app.core.audit import Auditor

    auditor = Auditor(mem_db, None, None)
    with caplog.at_level(logging.INFO, logger="audit"):
        auditor.log("parent.action", commit=False)
        with pytest.raises(ValueError), mem_db.begin_nested():
            auditor.log("rolled_back.child", commit=False)
            raise ValueError("rollback nested only")
        with mem_db.begin_nested():
            auditor.log("committed.child", commit=False)
        assert not [r for r in caplog.records if r.name == "audit"]
        mem_db.commit()
    assert [r.action for r in caplog.records if r.name == "audit"] == ["parent.action", "committed.child"]


def test_dependency_failure_is_rate_limited_closes_client_and_logs_recovery(monkeypatch, caplog):
    from unittest.mock import MagicMock

    from app.api.v1 import system

    client = MagicMock()
    client.__enter__.return_value = client
    client.ping.side_effect = [RuntimeError("redis://name:private-pw@internal/0"),
                               RuntimeError("redis://name:private-pw@internal/0"), True]
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("redis.Redis.from_url", factory)
    monkeypatch.setattr(system, "_redis_failed", False)
    monkeypatch.setattr(system, "_redis_next_log", 0.0)
    with caplog.at_level(logging.INFO, logger=system.__name__):
        assert system._redis_health("redis://local/0") is False
        assert system._redis_health("redis://local/0") is False
        assert system._redis_health("redis://local/0") is True
    rows = [r for r in caplog.records if r.name == system.__name__]
    assert [r.getMessage() for r in rows] == ["dependency.failed", "dependency.restored"]
    assert client.__exit__.call_count == 3
    assert "private-pw" not in "\n".join(logs.JsonFormatter().format(r) for r in rows)


def test_embedded_migration_does_not_turn_off_business_logger(migration_url):
    from alembic import command
    from alembic.config import Config

    logger = logging.getLogger("app.migration_logging_review")
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler, old_level, old_disabled = Capture(), logger.level, logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.disabled = False
    try:
        backend = Path(__file__).resolve().parents[1]
        config = Config(str(backend / "alembic.ini"))
        config.set_main_option("script_location", str(backend / "migrations"))
        config.attributes["database_url"] = migration_url
        command.upgrade(config, "head")
        logger.info("post_migration.business_log")
        assert not logger.disabled and records == ["post_migration.business_log"]
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.disabled = old_disabled
