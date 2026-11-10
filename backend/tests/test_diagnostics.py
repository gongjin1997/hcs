"""离线诊断的目录、脱敏、容量和归档边界；只使用临时目录。"""

import json
import os
import socket
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

from app.tools import diagnostics as tool

ROOT = Path(__file__).resolve().parents[2]


def logs(tmp_path):
    directory = tmp_path / "logs"
    directory.mkdir()
    return directory


def test_allowlist_metadata_redaction_and_no_raw_messages(tmp_path, monkeypatch):
    directory = logs(tmp_path)
    marker = "synthetic-private-value"
    record = {
        "ts": "2026-10-27T00:00:00Z",
        "level": "ERROR",
        "service": "api",
        "request_id": "correlation-1234",
        "method": "GET",
        "path": f"/api/v1/mail-actions/{marker}?token={marker}",
        "status": 500,
        "msg": f"request body {marker}",
        "exc": marker,
        "password": marker,
        "request_body": {"value": marker},
        "headers": {"Authorization": marker},
        "customer": marker,
    }
    (directory / "api.jsonl").write_text(json.dumps(record) + "\n" + marker + "\n")
    for filename in (".env", "config.json", "dev.db", "private.pem", "customer.log"):
        (directory / filename).write_text(marker)

    def denied(*_args, **_kwargs):
        raise AssertionError("离线工具不能联网或执行外部命令")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(subprocess, "run", denied)
    manifest, content = tool.collect(directory)
    assert len(content) == 1 and manifest["candidate_files"] == 1
    assert manifest["files"][0]["non_json_lines"] == 1
    encoded = content[0][1].decode()
    assert marker not in encoded and marker not in json.dumps(manifest)
    clean = json.loads(encoded)
    assert clean["status"] == 500 and clean["request_id"] == "correlation-1234"
    assert not {"msg", "exc", "password", "request_body", "headers", "customer"} & set(clean)
    assert "?" not in clean["path"] and "redact" in clean["path"].lower()
    assert str(tmp_path) not in json.dumps(manifest)


def test_bundle_keeps_scope_and_cloud_budget_metadata_without_invalid_ids():
    fields = {"tenant_id": 1, "project_ref": 2, "user_id": 3, "run_id": 9,
              "client_request_id": "browser-request-123", "upstream_request_id": "cloud-request-123",
              "cloud_service": "ces", "logical_path": "/V1.0/{project_id}/metrics?token=private-query",
              "api_calls": 12, "attempt": 2, "wait_ms": 100, "cache_status": "shared_hit"}
    clean = tool.sanitize_record(fields)
    assert clean["tenant_id"] == 1 and clean["project_ref"] == 2 and clean["run_id"] == 9
    assert clean["client_request_id"] == "browser-request-123" and clean["api_calls"] == 12
    assert clean["logical_path"] == "/V1.0/{project_id}/metrics"
    assert "private-query" not in json.dumps(clean)
    assert "tenant_id" not in tool.sanitize_record({"tenant_id": "private-data"})
    cloud = tool.sanitize_record({"rule_id": "R-DCS-001", "alarm_id": 4, "notification_id": 7,
                                 "cloud_services": {"ces": {"calls": 2, "errors": 0, "rate_limit_ms": 25,
                                                             "body": "private-content"},
                                                    "private value": {"calls": 3}}})
    assert cloud["rule_id"] == "R-DCS-001" and cloud["notification_id"] == 7
    assert cloud["cloud_services"] == {"ces": {"calls": 2, "errors": 0, "rate_limit_ms": 25}}
    assert "private" not in json.dumps(cloud)


def test_bundle_keeps_safe_collection_and_database_diagnostics():
    fields = {"event": "ledger_snapshot_failed", "resource_type": "dcs_instance", "phase": "database_apply",
              "run_id": 9, "record_count": 12, "fetch_calls": 2, "duration_ms": 18.5,
              "unknown_event_count": 2,
              "sqlstate": "22001", "db_column_limit": 128, "max_spec_chars": 150,
              "table_name": "resource", "column_name": "spec",
              "upstream_code": "APIGW.0201", "network_reason": "dns", "retryable": True,
              "sql": "private statement"}
    clean = tool.sanitize_record(fields)
    assert clean["record_count"] == 12 and clean["fetch_calls"] == 2
    assert clean["unknown_event_count"] == 2
    assert clean["sqlstate"] == "22001" and clean["column_name"] == "spec"
    assert clean["db_column_limit"] == 128 and clean["max_spec_chars"] == 150
    assert clean["upstream_code"] == "APIGW.0201" and clean["network_reason"] == "dns"
    assert "sql" not in clean and "private" not in json.dumps(clean)
    assert "column_name" not in tool.sanitize_record({"column_name": "password"})


def test_manual_worker_log_is_included(tmp_path):
    directory = logs(tmp_path)
    (directory / "run_once.jsonl").write_text('{"service":"run_once","event":"worker.failed","run_id":7}\n')
    manifest, content = tool.collect(directory)
    assert manifest["candidate_files"] == 1 and len(content) == 1
    assert json.loads(content[0][1])["run_id"] == 7


@pytest.mark.parametrize("kind", ["root_symlink", "ancestor_symlink", "parent_jump"])
def test_source_directory_must_not_follow_symlinks_or_parent_jumps(tmp_path, kind):
    real = logs(tmp_path)
    (real / "api.jsonl").write_text('{"level":"INFO"}\n')
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    path = alias if kind == "root_symlink" else alias / "nested" if kind == "ancestor_symlink" else real / ".." / "logs"
    with pytest.raises(tool.DiagnosticError):
        tool.collect(path)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_only_single_link_regular_files_are_read(tmp_path, kind):
    directory = logs(tmp_path)
    target = directory / "api.jsonl"
    secret = tmp_path / "synthetic-private-file"
    secret.write_text('{"level":"ERROR","msg":"not-for-export"}\n')
    if kind == "symlink":
        target.symlink_to(secret)
    elif kind == "hardlink":
        os.link(secret, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    manifest, content = tool.collect(directory)
    assert content == []
    assert manifest["files"][0]["skipped"] in {"unreadable_or_symlink", "not_regular_or_hardlinked"}


def test_size_count_and_line_budgets_are_enforced_and_recent_files_prioritized(tmp_path):
    directory = logs(tmp_path)
    for name in ("api.jsonl", "api.jsonl.1", "scheduler.jsonl", "web.jsonl"):
        (directory / name).write_text('{"service":"api","level":"INFO","status":200}\n' * 100)
    limits = tool.Limits(max_file_bytes=256, max_total_bytes=500, max_files=2, max_line_bytes=64)
    manifest, content = tool.collect(directory, limits)
    assert manifest["read_bytes"] <= 500 and manifest["log_bytes"] <= 500
    assert len(manifest["files"]) <= 2
    assert manifest["files"][0]["name"] == "api.jsonl"
    assert manifest["files"][1]["name"] == "scheduler.jsonl"
    assert all(report["truncated"] for report in manifest["files"])
    assert all(len(data) <= 256 for _, data in content)
    for _, data in content:
        assert all(isinstance(json.loads(line), dict) for line in data.splitlines())


def test_oversized_invalid_or_unknown_records_are_omitted(tmp_path):
    directory = logs(tmp_path)
    (directory / "api.jsonl").write_bytes(b"x" * 300 + b'\n[1,2]\n{"unknown":"value"}\n{"status":200}\n')
    manifest, content = tool.collect(directory, tool.Limits(max_line_bytes=100))
    report = manifest["files"][0]
    assert report["omitted_lines"] == 2 and report["non_json_lines"] == 1
    assert json.loads(content[0][1]) == {"status": 200}


def test_trace_keeps_only_technical_frames_and_type_not_source_or_message(tmp_path):
    directory = logs(tmp_path)
    marker = "synthetic-private-exception-value"
    trace = (
        "Traceback (most recent call last):\n"
        '  File "/private/customer/location/worker.py", line 27, in collect\n'
        f'    raise RuntimeError("{marker}")\nRuntimeError: {marker}'
    )
    (directory / "api.jsonl").write_text(json.dumps({"level": "ERROR", "exc": trace}) + "\n")
    _, content = tool.collect(directory)
    clean = json.loads(content[0][1])
    assert clean["frames"] == [{"file": "worker.py", "line": 27, "function": "collect"}]
    assert clean["exception_type"] == "RuntimeError"
    assert marker not in content[0][1].decode()
    assert "/private" not in content[0][1].decode() and "raise RuntimeError" not in content[0][1].decode()
    explicit = tool.sanitize_record({"exception_type": "ConnectTimeout", "exc": trace})
    assert explicit["exception_type"] == "ConnectTimeout" and explicit["frames"] == clean["frames"]
    early = tool.sanitize_record({"logger": "lan.stdout", "msg": '  File "/private/pkg/start.py", line 7, in <module>'})
    assert early["frames"] == [{"file": "start.py", "line": 7, "function": "<module>"}]
    last = tool.sanitize_record({"logger": "lan.stdout", "msg": "ModuleNotFoundError: private-module-name"})
    assert last["exception_type"] == "ModuleNotFoundError" and "private" not in json.dumps(last)


def test_trace_rejects_non_identifier_type_and_untrusted_frame_objects():
    clean = tool.sanitize_record(
        {
            "status": 500,
            "exception_type": "RuntimeError: synthetic-private",
            "frames": [{"file": "/private/file.py", "source": "synthetic-private"}],
            "exc": '  File "/private/secret.txt", line 1, in suspicious\nraw body',
        }
    )
    assert clean == {"status": 500}


def test_bundle_is_private_fixed_member_names_and_no_external_paths(tmp_path):
    directory = logs(tmp_path)
    (directory / "api.jsonl").write_text('{"level":"INFO","status":200}\n')
    output = tmp_path / "bundles"
    archive_path = tool.bundle(directory, output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(archive_path.stat().st_mode) == 0o600
    with tarfile.open(archive_path) as archive:
        assert archive.getnames() == ["manifest.json", "logs/api.jsonl.jsonl"]
        assert all(member.isfile() and member.mode == 0o600 and ".." not in member.name for member in archive)
        manifest = json.load(archive.extractfile("manifest.json"))
        assert manifest["offline"] is True and manifest["runtime"]["python"]
        assert str(directory) not in json.dumps(manifest)


def test_output_symlinks_and_existing_archive_are_not_overwritten(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 27, tzinfo=UTC)

    directory = logs(tmp_path)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(tool.DiagnosticError):
        tool.bundle(directory, alias)
    monkeypatch.setattr(tool.secrets, "token_hex", lambda _count: "fixed")
    monkeypatch.setattr(tool, "datetime", FixedTime)
    first = tool.bundle(directory, real)
    original = first.read_bytes()
    with pytest.raises(FileExistsError):
        tool.bundle(directory, real)
    assert first.read_bytes() == original


def test_existing_shared_output_directory_is_not_chmodded(tmp_path):
    directory = logs(tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(tool.DiagnosticError):
        tool.bundle(directory, shared)
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755
    assert list(shared.iterdir()) == []


@pytest.mark.parametrize(
    "limits",
    [
        tool.Limits(0),
        tool.Limits(max_files=101),
        tool.Limits(max_total_bytes=100),
        tool.Limits(max_file_bytes=11 * 1024 * 1024),
        tool.Limits(max_line_bytes=100000),
    ],
)
def test_invalid_budgets_are_rejected(limits):
    with pytest.raises(tool.DiagnosticError):
        limits.validate()


def test_cli_overview_and_failure_do_not_echo_source_contents(tmp_path, capsys):
    directory = logs(tmp_path)
    (directory / "api.log").write_text("synthetic-secret-line\n")
    assert tool.main(["overview", "--log-dir", str(directory)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["files"][0]["non_json_lines"] == 1
    assert tool.main(["overview", "--log-dir", str(tmp_path / "synthetic-secret-path")]) == 1
    assert "synthetic-secret" not in capsys.readouterr().err


def test_docker_logging_limits_and_safe_nginx_configuration():
    compose = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text())
    for service in compose["services"].values():
        assert service["logging"] == {"driver": "json-file", "options": {"max-size": "50m", "max-file": "5"}}
    assert compose["services"]["scheduler"]["stop_grace_period"] == "45s"
    demo = yaml.safe_load((ROOT / "deploy/docker-compose.demo.yml").read_text())
    assert all(demo["services"][name]["logging"]["options"]["max-file"] == "5" for name in ("mock-hcs", "mailpit"))
    configuration = (ROOT / "deploy/nginx/snippets/logging.conf").read_text()
    assert "escape=json" in configuration and "upstream_http_x_request_id" in configuration
    assert "/api/v1/mail-actions/[REDACTED]" in configuration and "/m/action/[REDACTED]" in configuration
    body = configuration.split("log_format hcs_json", 1)[1]
    for unsafe in ("$request_uri", "$args", "$query_string", "$request_body", "$http_authorization", "$http_x_request_id"):
        assert unsafe not in body
    for site in ("http.conf", "https.conf"):
        assert "include /etc/nginx/snippets/logging.conf;" in (ROOT / "deploy/nginx" / site).read_text()
