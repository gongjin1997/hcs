"""台账数据一致性、导入范围与每轮采集统计；测试只使用 mem_db。"""
import io
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select

from app.api.v1.resources import router
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.core.response import BizError
from app.ledger import importer, sync
from app.ledger.fetchers import LedgerRecord, fetch_ecs, fetch_evs
from app.models import HcsAccount, Resource, User
from app.services.pipeline import run_task
from app.services.settings_service import LedgerDict
from app.services.tenancy import Target

TARGET = Target(101, "review", "review", "review", 1001, "project", "project", "region")


class CountingClient:
    def __init__(self):
        self.calls = 7
    def configure_qps(self, _qps):
        pass
    def get(self, service, path, **_kwargs):
        self.calls += 1
        return {"instances": [{"instance_id": "one", "name": "one", "used_memory": 1, "max_memory": 2}],
                "instance_num": 1}
    def post(self, *_args, **_kwargs):
        self.calls += 1
        return {"metrics": []}
    def metrics_snapshot(self):
        return {"calls": self.calls, "errors": 0, "rate_limit_ms": 0,
                "services": {"dcs": {"calls": self.calls, "errors": 0, "rate_limit_ms": 0}}}


def test_missing_flavor_and_volume_size_are_not_synthetic_zero():
    client = SimpleNamespace(get=lambda service, *_a, **_k: {
        "servers": [{"id": "ecs", "name": "ecs"}], "count": 1} if service == "ecs" else {
        "volumes": [{"id": "vol", "name": "vol"}], "count": 1})
    server, = fetch_ecs(client, {})
    volume, = fetch_evs(client, {})
    assert server.vcpus is None and server.memory_gb is None
    assert volume.storage_gb is None


def test_missing_attribute_does_not_claim_a_change_that_was_not_applied(mem_db):
    row = Resource(service="dcs", resource_type="dcs_instance", resource_id="one", name="one",
                   attrs={"engine": "Redis"}, tenant_id=101, project_ref=1001)
    mem_db.add(row)
    mem_db.commit()
    result = sync.apply_snapshot(mem_db, "dcs_instance", [LedgerRecord("one", "one", attrs={"engine": None})],
                                 tenant_id=101, project_ref=1001)
    assert row.attrs["engine"] == "Redis" and result["updated"] == 0


def test_duplicate_snapshot_fails_closed_without_leaving_a_running_run(mem_db, monkeypatch):
    monkeypatch.setitem(sync.FETCHERS, "dcs_instance", lambda *_: [LedgerRecord("same", "same")] * 2)
    run = sync.sync_ledger(mem_db, client=CountingClient(), target=TARGET, types=["dcs_instance"])
    assert run.status == "FAILED" and run.finished_at is not None
    assert mem_db.scalars(select(Resource)).all() == []


def test_failed_resource_type_rolls_back_its_partial_writes(mem_db, monkeypatch):
    row = Resource(service="dcs", resource_type="dcs_instance", resource_id="one", name="original",
                   tenant_id=101, project_ref=1001)
    mem_db.add(row)
    mem_db.commit()
    monkeypatch.setitem(sync.FETCHERS, "dcs_instance", lambda *_: [LedgerRecord("one", "one")])
    def broken(db, *_args, **_kwargs):
        row.name = "partial-write"
        db.flush()
        raise ValueError("invalid cloud record")
    monkeypatch.setattr(sync, "apply_snapshot", broken)
    run = sync.sync_ledger(mem_db, client=CountingClient(), target=TARGET, types=["dcs_instance"])
    mem_db.refresh(row)
    assert run.status == "FAILED" and row.name == "original"


@pytest.mark.parametrize("kind", ["ledger", "pipeline"])
def test_reused_client_reports_only_calls_from_this_run(mem_db, kind):
    client = CountingClient()
    for _ in range(2):
        start = client.calls
        run = (sync.sync_ledger(mem_db, client=client, target=TARGET, types=["dcs_instance"]) if kind == "ledger"
               else run_task(mem_db, "dcs_status", client=client, target=TARGET))
        assert run.status == ("SUCCESS" if kind == "ledger" else "PARTIAL")  # 合成实例没有状态，不能记正常。
        assert run.api_calls == client.calls - start
        assert run.stats["hcs"]["calls"] == run.api_calls


def accounts(db):
    for tenant in (101, 202):
        db.add(HcsAccount(id=tenant, code=f"review-{tenant}", name=f"tenant-{tenant}",
                          domain_name="review", iam_endpoint="https://example.invalid"))
    db.flush()


def test_exported_template_roundtrips_same_tenant_same_cloud_id_in_two_projects(mem_db):
    accounts(mem_db)
    owner = User(username="new-owner", display_name="新负责人", password_hash="!", login_enabled=False, tenant_ids=[101])
    mem_db.add(owner)
    rows = [Resource(service="vpc", resource_type="vpc", resource_id="same", name=f"project-{project}",
                     tenant_id=101, project_ref=project) for project in (1001, 1002)]
    mem_db.add_all(rows)
    mem_db.commit()
    wb = load_workbook(io.BytesIO(importer.template(mem_db, ["vpc"], [101])))
    ws = wb["台账管理字段"]
    headers = [cell.value for cell in ws[1]]
    ws.cell(2, headers.index("负责人") + 1, owner.display_name)
    ws.cell(2, headers.index("负责人账号") + 1, owner.username)
    output = io.BytesIO()
    wb.save(output)
    wb.close()
    result, todo = importer.parse(mem_db, output.getvalue(), [101], LedgerDict())
    assert not result.errors and len(todo) == 1
    assert todo[0][0].project_ref == 1001
    importer.apply(mem_db, todo, "review")
    assert (rows[0].owner, rows[0].owner_user_id) == (owner.display_name, owner.id)
    assert rows[1].owner is None and rows[1].owner_user_id is None


def test_import_api_dry_run_and_apply_cannot_escape_scope(mem_db):
    accounts(mem_db)
    owner = User(username="review-owner", display_name="导入负责人", password_hash="!", login_enabled=False)
    mem_db.add(owner)
    mine = Resource(service="vpc", resource_type="vpc", resource_id="shared", name="mine", tenant_id=101, project_ref=1001)
    other = Resource(service="vpc", resource_type="vpc", resource_id="shared", name="other", tenant_id=202, project_ref=2002)
    mem_db.add_all([mine, other])
    mem_db.commit()
    wb = load_workbook(io.BytesIO(importer.template(mem_db, ["vpc"], None)))
    ws = wb["台账管理字段"]
    headers = [cell.value for cell in ws[1]]
    for index in (2, 3):
        ws.cell(index, headers.index("负责人") + 1, owner.display_name)
        ws.cell(index, headers.index("负责人账号") + 1, owner.username)
    output = io.BytesIO()
    wb.save(output)
    wb.close()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=1, username="review", display_name="review", permissions=["ledger:edit", "ledger:view"], tenant_ids=[101])
    @app.exception_handler(BizError)
    async def error(_request, exc):
        return JSONResponse(status_code=exc.http_status, content={"code": exc.code, "message": exc.message, "data": exc.data})
    with TestClient(app) as client:
        response = client.post("/api/v1/resources/import", files={"file": ("review.xlsx", output.getvalue())})
        assert response.status_code == 200 and response.json()["data"]["error_count"] == 1
        assert mine.owner is None and other.owner is None
        assert mine.owner_user_id is None and other.owner_user_id is None
        response = client.post("/api/v1/resources/import", params={"dry_run": "false", "skip_errors": "true"},
                               files={"file": ("review.xlsx", output.getvalue())})
        assert response.status_code == 200 and response.json()["data"]["applied"] == 1
    assert (mine.owner, mine.owner_user_id) == (owner.display_name, owner.id)
    assert other.owner is None and other.owner_user_id is None


def test_direct_task_respects_closed_collection_before_calling_cloud(mem_db):
    from app.models import CollectSwitch
    mem_db.add(CollectSwitch(tenant_id=101, resource_type="dcs_instance", enabled=False))
    mem_db.commit()
    client = CountingClient()
    before = client.calls
    run = run_task(mem_db, "dcs_status", client=client, target=TARGET)
    assert run.status == "SKIPPED" and run.api_calls == 0
    assert client.calls == before
    assert mem_db.scalars(select(Resource)).all() == []


def test_process_observations_cannot_write_a_disabled_resource_type(mem_db):
    from app.collectors.base import Observation, RunContext
    from app.models import CollectSwitch
    from app.models.base import utcnow
    from app.services.pipeline import process_observations
    mem_db.add(CollectSwitch(tenant_id=101, resource_type="dcs_instance", enabled=False))
    mem_db.commit()
    ctx = RunContext(client=None, account="review", project_id="project", tenant_id=101, project_ref=1001)
    process_observations(mem_db, "dcs", [Observation("dcs_instance", "off", "off", "instance_status",
                                                  {"status": "RUNNING"}, status_raw="RUNNING")], ctx, utcnow())
    assert mem_db.scalars(select(Resource)).all() == []


@pytest.mark.parametrize("state", ["PARTIAL", "SKIPPED"])
def test_sync_api_preserves_non_success_run_status(mem_db, monkeypatch, state):
    from unittest.mock import MagicMock

    from app.api.v1 import resources
    from app.core.deps import Scope
    from app.models import InspectionRun
    run = InspectionRun(task_code="ledger_sync", tenant_id=101, status=state, resource_total=0, abnormal_total=0, stats={})
    monkeypatch.setattr(resources, "sync_all", lambda *_a, **_k: [run])
    user = CurrentUser(id=1, username="review", display_name="review", permissions=["ledger:sync"], tenant_ids=[101])
    result = resources.sync_now(resource_type="dcs_instance", db=mem_db, scope=Scope([101], False, user),
                                audit=MagicMock(), _=user)
    assert result["data"]["status"] == state


def test_empty_scope_does_not_fall_back_to_legacy_collection(mem_db, monkeypatch):
    import app.services.tenancy as tenancy
    from app.services import pipeline
    def no_legacy():
        pytest.fail("scoped operation must not construct a global legacy target")
    monkeypatch.setattr(tenancy, "legacy_target", no_legacy)
    assert pipeline.run_all(mem_db, "dcs_status", tenant_ids=[]) == []
    assert sync.sync_all(mem_db, tenant_ids=[]) == []
