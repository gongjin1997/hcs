"""监控名单权限、精确隔离、零查询与大清单分页；仅独立内存库/云客户端替身。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, select, text
from sqlalchemy.orm import sessionmaker

from app.api.v1.system import router
from app.collectors.base import Observation, RunContext
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.core.response import BizError
from app.hcs import HcsError
from app.models import AlarmEvent, HcsAccount, HcsProject, Resource, SysSetting
from app.schemas.job_selection import JobSelectionIn
from app.services import collect_scope, job_selection

RT = "dgc_realtime_job"
BATCH = "dgc_batch_job"


@pytest.fixture
def configured_db(mem_db):
    for tid in (11, 22):
        mem_db.add(HcsAccount(id=tid, code=f"t-{tid}", name=f"Tenant {tid}", iam_endpoint="https://invalid.test",
                             domain_name="synthetic"))
        for pid in ((101, 102) if tid == 11 else (201,)):
            mem_db.add(HcsProject(id=pid, account_id=tid, region_id="synthetic", project_id=f"p-{pid}",
                                 external_domain="invalid.test", workspaces=["ws-a", "ws-b"]))
    mem_db.commit()
    return mem_db


def body(*jobs, project=101, workspace="ws-a", mode="selected"):
    return JobSelectionIn(project_ref=project, workspace=workspace, mode=mode, jobs=list(jobs))


def context(db, project=101, tenant=11, rtype=RT):
    return RunContext(client=None, account="synthetic", project_id=f"p-{project}", tenant_id=tenant,
                      project_ref=project, workspaces=["ws-a", "ws-b"], db=db, scopes=collect_scope.load(db))


def test_unconfigured_inherits_existing_scope_but_selected_empty_denies_before_any_query(configured_db):
    db = configured_db
    ctx = context(db)
    inherited = job_selection.scope_for_context(ctx, RT)
    assert inherited.open and inherited.keep({"workspace": "ws-a", "job": "legacy"})
    assert ctx.stats["job_selection"]["workspaces"][0]["reason"] == "inherited_scope"
    job_selection.save(db, 11, RT, body(), "tester")
    db.commit()
    ctx = context(db)
    selected = job_selection.scope_for_context(ctx, RT)
    calls = []
    observations = []
    if selected.keep({"workspace": "ws-a"}):
        calls.append("list")
        observations.append("unexpected")
    assert calls == observations == []
    assert not selected.open and not selected.keep({"workspace": "ws-a", "job": "anything"})
    assert ctx.stats["job_selection"]["workspaces"][0]["reason"] == "no_selected_jobs"
    assert selected.keep({"workspace": "ws-b", "job": "legacy"})  # 未配置的另一个空间保持兼容。


def test_exact_selected_names_do_not_cross_projects_workspaces_types_or_tenants(configured_db):
    db = configured_db
    job_selection.save(db, 11, RT, body("job-a", "job-*"), "tester")
    selected = job_selection.scope_for_context(context(db), RT)
    assert selected.keep({"workspace": "ws-a"})
    assert selected.keep({"workspace": "ws-a", "job": "job-a"})
    assert selected.keep({"workspace": "ws-a", "job": "job-*"})
    assert not selected.keep({"workspace": "ws-a", "job": "job-other"})
    assert job_selection.read(db, 11, RT, 102, "ws-a")["mode"] == "inherit"
    assert job_selection.read(db, 22, RT, 201, "ws-a")["mode"] == "inherit"
    assert job_selection.read(db, 11, BATCH, 101, "ws-a")["mode"] == "inherit"
    assert job_selection.read(db, 11, RT, 101, "ws-b")["mode"] == "inherit"


def test_generic_empty_rules_cannot_open_an_explicit_empty_selection(configured_db):
    db = configured_db
    job_selection.save(db, 11, RT, body(), "tester")
    collect_scope.save(db, 11, RT, [], "tester")
    db.commit()
    assert not job_selection.scope_for_context(context(db), RT).keep({"workspace": "ws-a"})
    assert job_selection.read(db, 11, RT, 101, "ws-a")["mode"] == "selected"


@pytest.mark.parametrize("value", [{}, {"version": 1, "workspaces": {"ws-a": {"jobs": []}}},
    {"version": 1, "workspaces": {"ws-a": {"mode": "all", "jobs": ["x"]}}},
    {"version": 1, "workspaces": {"ws-a": {"mode": "selected", "jobs": [None]}}}])
def test_malformed_saved_selection_fails_closed(value):
    selected = job_selection.JobSelectionScope(collect_scope.OPEN, value)
    assert not selected.keep({"workspace": "ws-a"})
    assert not selected.keep({"workspace": "ws-a", "job": "job-a"})


def test_selection_intersects_existing_generic_exclusions(configured_db):
    db = configured_db
    collect_scope.save(db, 11, RT, [{"effect": "exclude", "match": {"job": ["job-a"]}}], "tester")
    job_selection.save(db, 11, RT, body("job-a", "job-b"), "tester")
    selected = job_selection.scope_for_context(context(db), RT)
    assert not selected.keep({"workspace": "ws-a", "job": "job-a"})
    assert selected.keep({"workspace": "ws-a", "job": "job-b"})


def test_save_materializes_scoped_flags_closes_only_excluded_alarms_and_never_changes_switches(configured_db):
    db = configured_db
    resources = []
    for pid, ws, name in [(101, "ws-a", "keep"), (101, "ws-a", "drop"),
                          (101, "ws-b", "other-ws"), (102, "ws-a", "other-project")]:
        res = Resource(tenant_id=11, project_ref=pid, service="dataarts", resource_type=RT,
                       resource_id=f"{ws}/{name}", name=name, attrs={"workspace": ws}, health="OK")
        db.add(res)
        db.flush()
        resources.append(res)
    dropped = resources[1]
    alarm = AlarmEvent(tenant_id=11, fingerprint="job-selection-test", rule_id="test", resource_ref=dropped.id,
                       service="dataarts", resource_type=RT, resource_name="drop", check_item="job_status",
                       severity="P2", status="FIRING", title="synthetic")
    db.add(alarm)
    db.commit()
    result = job_selection.save(db, 11, RT, body("keep"), "tester")
    db.commit()
    assert result["excluded"] == result["closed_alarms"] == 1
    assert [r.scope_excluded for r in resources] == [False, True, False, False]
    assert alarm.status == "CLOSED"
    assert list(db.scalars(select(SysSetting.key))) == [job_selection.setting_key(11, 101, RT)]
    # 通用规则再次同步不能撤掉作业名单的范围外标记。
    collect_scope.save(db, 11, RT, [], "tester")
    assert dropped.scope_excluded
    assert collect_scope.preview(db, 11, RT, [])["excluded"] == 1


def test_final_pipeline_filter_uses_project_and_cannot_restore_unselected_job(configured_db):
    db = configured_db
    job_selection.save(db, 11, RT, body("approved"), "tester")
    def group(name):
        return [Observation(RT, f"ws-a/{name}", name, "job_status", {}, attrs={"workspace": "ws-a"})]
    groups = {(RT, "ws-a/approved"): group("approved"), (RT, "ws-a/no"): group("no")}
    scopes = collect_scope.load(db)
    assert set(collect_scope.filter_groups(db, 11, groups, {}, scopes, project_ref=101)) == {(RT, "ws-a/approved")}
    assert len(collect_scope.filter_groups(db, 11, groups, {}, scopes, project_ref=102)) == 2
    assert collect_scope.filter_groups(db, 11, groups, {}, scopes) == {}


def test_guard_detects_changed_policy_even_when_general_rules_stay_empty(configured_db):
    db = configured_db
    ctx = context(db)
    job_selection.scope_for_context(ctx, RT)
    assert not job_selection.selection_changed(ctx)
    job_selection.save(db, 11, RT, body("approved"), "tester")
    db.commit()
    assert job_selection.selection_changed(ctx)
    ctx = context(db)
    job_selection.scope_for_context(ctx, RT)
    job_selection.save(db, 11, RT, body("other", project=102), "tester")
    db.commit()
    assert not job_selection.selection_changed(ctx)


def test_revision_detects_change_then_restore_and_identical_save_preserves_revision(configured_db):
    db = configured_db
    job_selection.save(db, 11, RT, body("job-a"), "tester")
    db.commit()
    key = job_selection.setting_key(11, 101, RT)
    first = db.get(SysSetting, key).value["revision"]
    assert UUID(first).version == 4
    ctx = context(db)
    job_selection.scope_for_context(ctx, RT)
    result = job_selection.save(db, 11, RT, body("job-a"), "tester")
    db.commit()
    assert not result["changed"] and db.get(SysSetting, key).value["revision"] == first
    assert not job_selection.selection_changed(ctx)
    job_selection.save(db, 11, RT, body("job-b"), "tester")
    db.commit()
    job_selection.save(db, 11, RT, body("job-a"), "tester")
    db.commit()
    assert db.get(SysSetting, key).value["revision"] != first
    assert job_selection.selection_changed(ctx)


@pytest.mark.pg
def test_pg_first_saves_of_two_workspaces_are_serialized_by_existing_tenant(configured_db):
    db = configured_db
    sessions = sessionmaker(bind=db.get_bind(), expire_on_commit=False, autoflush=False)
    start = Barrier(2)

    def write(workspace, job):
        with sessions() as session:
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            start.wait(timeout=3)
            job_selection.save(session, 11, RT, body(job, workspace=workspace), "concurrent-editor")
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write, "ws-a", "job-a")
        second = pool.submit(write, "ws-b", "job-b")
        first.result(timeout=8)
        second.result(timeout=8)
    assert job_selection.read(db, 11, RT, 101, "ws-a")["jobs"] == ["job-a"]
    assert job_selection.read(db, 11, RT, 101, "ws-b")["jobs"] == ["job-b"]
    assert list(db.scalars(select(SysSetting.key))) == [job_selection.setting_key(11, 101, RT)]


@pytest.mark.pg
def test_pg_final_guard_holds_parent_lock_until_persist_commit_then_save_excludes_old_result(configured_db):
    db = configured_db
    job_selection.save(db, 11, RT, body("job-a"), "tester")
    db.commit()
    engine = db.get_bind()
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    waiting, finished = Event(), Event()

    def before_cursor(_connection, _cursor, statement, _params, _execution_context, _many):
        if "hcs_account" in statement and "FOR UPDATE" in statement:
            waiting.set()

    def write():
        with sessions() as session:
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            job_selection.save(session, 11, RT, body("job-b"), "concurrent-editor")
            session.commit()
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool, sessions() as runner:
        ctx = context(runner)
        job_selection.scope_for_context(ctx, RT)
        assert not job_selection.selection_changed(ctx)
        event.listen(engine, "before_cursor_execute", before_cursor)
        try:
            saving = pool.submit(write)
            assert waiting.wait(timeout=2)
            assert not finished.wait(timeout=0.15), "save crossed the guard before pipeline committed"
            runner.add(Resource(tenant_id=11, project_ref=101, service="dataarts", resource_type=RT,
                                resource_id="ws-a/job-a", name="job-a", attrs={"workspace": "ws-a"}))
            runner.commit()
            saving.result(timeout=5)
        finally:
            runner.rollback()
            event.remove(engine, "before_cursor_execute", before_cursor)
    assert job_selection.read(db, 11, RT, 101, "ws-a")["jobs"] == ["job-b"]
    assert db.scalar(select(Resource.scope_excluded).where(Resource.resource_id == "ws-a/job-a")) is True


def test_dataarts_mrs_node_scope_sync_reads_workspace_job_and_node_attrs(configured_db):
    db = configured_db
    node = Resource(tenant_id=11, project_ref=101, service="dataarts", resource_type="dgc_mrs_node",
                    resource_id="ws-a/job/node", name="job / node", health="OK",
                    attrs={"workspace": "ws-a", "job_name": "job", "node_name": "node"})
    db.add(node)
    db.commit()
    collect_scope.save(db, 11, "dgc_mrs_node", [
        {"effect": "include", "match": {"workspace": ["ws-a"], "job": ["job"], "node": ["node"]}},
    ], "tester")
    db.commit()
    assert not node.scope_excluded


def test_pipeline_discards_observations_when_approved_selection_changes(configured_db, monkeypatch):
    from app.services import pipeline
    from app.services.tenancy import Target

    db = configured_db

    class NoCloud:
        calls = 0

        def configure_qps(self, _qps):
            pass

        def metrics_snapshot(self):
            return {"calls": 0, "errors": 0, "rate_limit_ms": 0, "services": {}}

    def collect(ctx):
        job_selection.scope_for_context(ctx, RT)
        job_selection.save(db, 11, RT, body("new-approved-job"), "another-request")
        db.commit()
        return [Observation(RT, "ws-a/old-job", "old-job", "job_status", {}, attrs={"workspace": "ws-a"})]

    monkeypatch.setitem(pipeline.TASKS, "dgc_realtime_status", SimpleNamespace(service="dataarts", collect=collect))
    monkeypatch.setattr(pipeline.platform_watch, "on_run", lambda *_args: None)
    target = Target(11, "synthetic", "Synthetic", "Synthetic", 101, "p-101", "Project", "synthetic", ["ws-a"])
    run = pipeline.run_task(db, "dgc_realtime_status", client=NoCloud(), target=target)
    assert run.status == "SKIPPED" and run.stats["reason"] == "policy_changed"
    assert list(db.scalars(select(Resource))) == []


class FakeCloud:
    def __init__(self, rows):
        self.rows, self.calls = rows, []
        self.http = SimpleNamespace(close=lambda: None)

    def get(self, service, path, **kwargs):
        self.calls.append((service, path, kwargs))
        assert service == "dataarts" and path == "/v1/{project_id}/jobs"
        params = kwargs["params"]
        begin = params["offset"] * params["limit"]
        return {"total": len(self.rows), "jobs": self.rows[begin:begin + params["limit"]]}


def test_discovery_pages_large_definition_list_without_status_or_definition_calls(configured_db):
    cloud = FakeCloud([{"name": f"job-{i}", "jobType": "REAL_TIME"} for i in range(251)])
    found = []
    for page in (1, 2, 3):
        result = job_selection.discover(configured_db, 11, RT, 101, "ws-a", page=page, page_size=100, client=cloud)
        assert result["total"] == 251
        found += result["items"]
    assert len(found) == 251
    assert [call[2]["params"]["offset"] for call in cloud.calls] == [0, 1, 2]
    assert all(call[2]["headers"] == {"workspace": "ws-a"} for call in cloud.calls)


def test_discovery_failure_is_not_an_empty_job_list(configured_db):
    cloud = FakeCloud([])
    cloud.get = lambda *_args, **_kwargs: {"total": 5, "jobs": []}
    with pytest.raises(HcsError):
        job_selection.discover(configured_db, 11, BATCH, 101, "ws-a", page=1, page_size=100, client=cloud)


@pytest.mark.parametrize("kwargs", [{"jobs": [""]}, {"jobs": [" x"]}, {"jobs": [7]},
                                    {"mode": "all", "jobs": ["x"]}, {"mode": None}])
def test_invalid_or_implicit_payload_is_rejected(kwargs):
    with pytest.raises(ValidationError):
        JobSelectionIn(**{"project_ref": 101, "workspace": "ws-a", "mode": "selected", "jobs": [], **kwargs})


def api(db, permissions=("collect:manage", "task:view")):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=1, username="tester", display_name="Tester", role_code="operator", role_name="Operator",
        permissions=list(permissions), tenant_ids=[11])

    @app.exception_handler(BizError)
    async def handle(_request, exc):
        return JSONResponse(status_code=exc.http_status, content={"code": int(exc.code), "message": exc.message})

    return TestClient(app)


def test_api_enforces_permission_tenant_project_workspace_and_requires_explicit_mode(configured_db, monkeypatch):
    monkeypatch.setattr(job_selection, "client_for", lambda *_args: pytest.fail("Forbidden request reached cloud"))
    client = api(configured_db)
    assert [t["id"] for t in client.get("/system/job-selections/targets").json()["data"]] == [11]
    path = f"/system/job-selections/11/{RT}"
    assert client.get(path, params={"project_ref": 201, "workspace": "ws-a"}).status_code == 404
    assert client.get(path, params={"project_ref": 101, "workspace": "other"}).status_code == 400
    assert client.get(path.replace("/11/", "/22/"), params={"project_ref": 201, "workspace": "ws-a"}).status_code == 403
    assert client.get(path + "/jobs", params={"project_ref": 201, "workspace": "ws-a"}).status_code == 404
    payload = {"project_ref": 101, "workspace": "ws-a", "mode": "selected", "jobs": []}
    result = client.put(path, json=payload)
    assert result.status_code == 200 and result.json()["data"]["after"]["mode"] == "selected"
    assert client.get(path, params={"project_ref": 101, "workspace": "ws-a"}).json()["data"]["jobs"] == []
    assert client.put(path, json={k: v for k, v in payload.items() if k != "mode"}).status_code == 422
    viewer = api(configured_db, ("task:view",))
    assert viewer.put(path, json=payload).status_code == 403
    assert viewer.get(path + "/jobs", params={"project_ref": 101, "workspace": "ws-a"}).status_code == 403
