"""网络工具选取正式配置，但不读取凭据，不跨租户使用项目。"""
import importlib.util
import json
from contextlib import nullcontext
from pathlib import Path

from sqlalchemy import event, select

from app.core import db as db_module
from app.core.config import get_settings
from app.models import HcsProject


def _probe():
    path = Path(__file__).resolve().parents[2] / "deploy/scripts/network-check.py"
    spec = importlib.util.spec_from_file_location("service_network_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_services_cli_uses_configured_endpoints_without_selecting_credentials(world, mem_db, monkeypatch, capsys):
    probe = _probe()
    tenant = world["tenant-b"]
    tenant.iam_endpoint = "https://iam.example.invalid"
    tenant.endpoints = {"ecs": "https://ecs-override.example.invalid"}
    project = mem_db.scalar(select(HcsProject).where(HcsProject.account_id == tenant.id))
    project.region_id, project.external_domain = "region-qa", "example.invalid"
    mem_db.commit()
    monkeypatch.setattr(db_module, "SessionLocal", lambda: nullcontext(mem_db))
    monkeypatch.setattr(get_settings(), "hcs_verify", False)
    requested, statements = [], []

    def check(url, **kwargs):
        requested.append((url, kwargs))
        return {"ok": True, "endpoint": url, "http": {"status": 401, "authentication_tested": False}}

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    monkeypatch.setattr(probe, "check", check)
    event.listen(mem_db.get_bind(), "before_cursor_execute", observe)
    try:
        assert probe.main(["--tenant", str(tenant.id), "--services", "all"]) == 0
    finally:
        event.remove(mem_db.get_bind(), "before_cursor_execute", observe)
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["project_ref"] == project.id
    assert [item["service"] for item in result["services"]] == [
        "iam", "ecs", "cce", "dcs", "gaussdb", "mrs", "dataarts", "evs", "vpc", "elb", "dws"]
    assert requested[0][0] == "https://iam.example.invalid"
    assert requested[1][0] == "https://ecs-override.example.invalid"
    assert all(options == {"verify": False, "ca_file": None} for _, options in requested)
    assert "secret_cipher" not in " ".join(statements) and "access_key" not in " ".join(statements)
    assert not result["authentication_tested"] and not result["configuration_modified"]


def test_services_cli_rejects_another_tenants_project(world, mem_db, monkeypatch, capsys):
    probe = _probe()
    other = mem_db.scalar(select(HcsProject).where(HcsProject.account_id == world["tenant-a"].id))
    monkeypatch.setattr(db_module, "SessionLocal", lambda: nullcontext(mem_db))
    calls = []
    monkeypatch.setattr(probe, "check", lambda *args, **kwargs: calls.append(args))
    assert probe.main(["--tenant", str(world["tenant-b"].id), "--project-ref", str(other.id),
                       "--services", "all"]) == 2
    assert "找不到指定的启用项目" in capsys.readouterr().out
    assert not calls
