"""Durable admission, scoped execution views and local cancellation through real HTTP APIs."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select

from app.api.v1 import executions
from app.models import AuditLog, HcsAccount, HcsProject, InspectionRun, InspectionTask, SysSetting
from app.models.base import utcnow
from app.models.task_execution import TaskExecution, TaskExecutionCancel
from app.services import task_execution

from . import test_user_tenant_review

management_api = test_user_tenant_review.management_api


@pytest.fixture()
def execution_api(mem_db, management_api, monkeypatch):
    from app.hcs import HcsClient
    from app.services import pipeline

    client, user, _, _ = management_api
    client.app.include_router(executions.router, prefix="/api/v1")
    user.permissions = ["task:view", "task:run", "task:edit"]
    mem_db.add(InspectionTask(code="dcs_status", name="缓存巡检", service="dcs", interval_sec=120, enabled=True,
                              params={"timeout_sec": 60, "secret": "do-not-expose", "custom": {"owner_token": "hidden"}}))
    mem_db.commit()

    def forbid_cloud(*args, **kwargs):
        pytest.fail("Execution HTTP API must never invoke cloud collection")

    monkeypatch.setattr(HcsClient, "get", forbid_cloud)
    monkeypatch.setattr(pipeline, "run_all", forbid_cloud)
    yield client, user


def enqueue(db, *, code="dcs_status", tenant=1, project=11, key=None):
    result = task_execution.enqueue(db, code, [tenant], project_refs=[project], request_key=key or uuid4().hex)
    return db.get(TaskExecution, result["execution_ids"][0])


def complete(db, row, status):
    task_execution.finish(db, row, status, "local execution outcome")
    db.commit()


def test_run_accepts_persisted_queue_with_optional_body_and_safe_dto(mem_db, execution_api):
    client, user = execution_api
    user.tenant_ids = [1]
    response = client.post("/api/v1/system/tasks/dcs_status/run")
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["status"] == "QUEUED" and len(data["execution_ids"]) == 1
    row = mem_db.get(TaskExecution, data["execution_ids"][0])
    assert row is not None and row.status == "QUEUED" and row.project_ref == 11 and row.tenant_id == 1
    dto = data["runs"][0]
    assert dto["id"] == dto["execution_id"] == row.id and dto["run_id"] == row.run_id
    assert dto["started_at"] is None and dto["elapsed_sec"] == 0
    assert dto["phase"] == "queued" and dto["phase_label"] == "排队等待"
    assert dto["timeout_sec"] == 60 and dto["queued_at"] == dto["created_at"]
    assert dto["tenant_name"] == "租户1" and dto["project_label"] == "project-11"
    assert not {"params", "target", "owner_token", "worker_id", "request_key", "lease_expires_at"} & dto.keys()
    assert "do-not-expose" not in response.text
    audit = mem_db.scalar(select(AuditLog).where(AuditLog.action == "task.enqueue"))
    assert audit is not None and audit.detail["outcome"] == "QUEUED"
    assert mem_db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "task.run")) == 0


def test_idempotency_and_active_target_deduplication_are_returned_as_dtos(mem_db, execution_api):
    client, user = execution_api
    user.tenant_ids = [1]
    body = {"project_refs": [11], "idempotency_key": "safe-client-key"}
    first = client.post("/api/v1/system/tasks/dcs_status/run", json=body).json()["data"]
    repeated = client.post("/api/v1/system/tasks/dcs_status/run", json=body)
    assert repeated.status_code == 202 and repeated.json()["data"]["deduplicated"] is True
    assert repeated.json()["data"]["execution_ids"] == first["execution_ids"]
    other_key = client.post("/api/v1/system/tasks/dcs_status/run", json={**body, "idempotency_key": "another-key"})
    assert other_key.json()["data"]["execution_ids"] == first["execution_ids"]
    assert mem_db.scalar(select(func.count()).select_from(TaskExecution)) == 1


@pytest.mark.parametrize("body", [
    {"project_refs": []}, {"project_refs": [True]}, {"project_refs": ["11"]}, {"project_refs": [11, 11]},
    {"project_refs": [0]}, {"force": True}, {"tenant_ids": [2]}, {"idempotency_key": ""},
])
def test_run_rejects_ambiguous_or_unpublished_inputs(mem_db, execution_api, body):
    client, _ = execution_api
    assert client.post("/api/v1/system/tasks/dcs_status/run", json=body).status_code == 400
    assert mem_db.scalar(select(func.count()).select_from(TaskExecution)) == 0


def test_run_authorizes_each_selected_project_before_enqueue(mem_db, execution_api):
    client, user = execution_api
    user.tenant_ids = [1]
    for refs in ([21], [11, 21], [98765]):
        response = client.post("/api/v1/system/tasks/dcs_status/run", json={"project_refs": refs})
        assert response.status_code == 404, response.text
    assert mem_db.scalar(select(func.count()).select_from(TaskExecution)) == 0
    response = client.post("/api/v1/system/tasks/dcs_status/run", json={"project_refs": None})
    assert response.status_code == 202 and {r["tenant_id"] for r in response.json()["data"]["runs"]} == {1}


def test_empty_authorized_scope_and_disabled_task_never_expand_targets(mem_db, execution_api):
    client, user = execution_api
    user.tenant_ids = []
    assert client.post("/api/v1/system/tasks/dcs_status/run").status_code == 409
    assert mem_db.scalar(select(func.count()).select_from(TaskExecution)) == 0
    user.tenant_ids = [1]
    mem_db.get(InspectionTask, "dcs_status").enabled = False
    mem_db.commit()
    assert client.post("/api/v1/system/tasks/dcs_status/run").status_code == 409


def test_execution_listing_filters_before_count_and_pagination(mem_db, execution_api):
    client, user = execution_api
    first = enqueue(mem_db)
    complete(mem_db, first, "FAILED")
    latest = enqueue(mem_db)
    hidden = enqueue(mem_db, tenant=2, project=21)
    user.tenant_ids = [1]
    response = client.get("/api/v1/system/executions", params={"page_size": 1})
    data = response.json()["data"]
    assert response.status_code == 200 and data["total"] == 2 and len(data["items"]) == 1
    assert data["items"][0]["id"] == latest.id
    filters = {"task_code": "dcs_status", "tenant_id": 1, "project_ref": 11, "status": "FAILED"}
    filtered = client.get("/api/v1/system/executions", params=filters).json()["data"]
    assert filtered["total"] == 1 and filtered["items"][0]["id"] == first.id
    assert client.get(f"/api/v1/system/executions/{hidden.id}").status_code == 404
    assert client.get("/api/v1/system/executions", params={"tenant_id": 2}).status_code == 404
    assert client.get("/api/v1/system/executions", params={"project_ref": 21}).json()["data"]["total"] == 0
    assert client.get("/api/v1/system/executions", params={"status": "SUCCEEDED"}).status_code == 400
    assert client.get("/api/v1/system/executions", params={"page_size": 101}).status_code == 400
    assert client.get("/api/v1/system/executions", params={"page": 0}).status_code == 400


def test_switching_tenant_and_historical_targets_do_not_leak_scope(mem_db, execution_api):
    client, user = execution_api
    first = enqueue(mem_db)
    second = enqueue(mem_db, tenant=2, project=21)
    selected = client.get("/api/v1/system/executions", headers={"X-Tenant-Id": "2"}).json()["data"]
    assert selected["total"] == 1 and selected["items"][0]["id"] == second.id
    assert client.get(f"/api/v1/system/executions/{first.id}", headers={"X-Tenant-Id": "2"}).status_code == 404
    mem_db.delete(mem_db.get(HcsProject, 11))
    mem_db.commit()
    user.tenant_ids = [1]
    historical = client.get("/api/v1/system/executions", params={"project_ref": 11}).json()["data"]
    assert historical["total"] == 1 and historical["items"][0]["project_label"] == "project-11"


def test_detail_whitelists_progress_history_and_snapshot_fields(mem_db, execution_api):
    client, _ = execution_api
    row = enqueue(mem_db)
    row.status, row.owner_token, row.worker_id = "RUNNING", "never-return-owner", "private-worker"
    row.started_at = utcnow() - timedelta(seconds=17)
    row.heartbeat_at, row.deadline_at = utcnow(), utcnow() + timedelta(seconds=43)
    row.progress = {"phase": "collect", "phase_label": "采集资源", "current": 2, "total": 8, "unit": "集群",
                    "message": "page processed", "owner_token": "never-return-owner", "params": {"secret": "hidden"}}
    row.history = [{"phase": "auth", "message": "password=hidden-value", "headers": {"X-Auth-Token": "hidden"}},
                   {"phase": "collect", "current": 2, "total": 8, "target": {"secret": "hidden"}}]
    run = mem_db.get(InspectionRun, row.run_id)
    run.api_calls, run.resource_total, run.abnormal_total = 4, 9, 1
    mem_db.add(TaskExecutionCancel(execution_id=row.id, requested_by="operator"))
    mem_db.commit()
    response = client.get(f"/api/v1/system/executions/{row.id}")
    data = response.json()["data"]
    assert data["phase_label"] == "采集资源" and data["current"] == 2 and data["total"] == 8
    assert data["elapsed_sec"] >= 17 and data["api_calls"] == 4 and data["resource_total"] == 9
    assert data["abnormal_total"] == 1 and data["cancel_requested"] is True
    for value in ("never-return-owner", "private-worker", "hidden-value", "X-Auth-Token"):
        assert value not in response.text
    assert data["history"][0]["phase"] == "auth" and data["history"][0]["phase_label"] == "认证"
    assert set(data["history"][0]) == {"phase", "phase_label", "message"}


def test_cancel_and_retry_authorize_before_calling_internal_service(mem_db, execution_api, monkeypatch):
    client, user = execution_api
    row = enqueue(mem_db, tenant=2, project=21)
    user.tenant_ids = [1]

    def deny(*args, **kwargs):
        pytest.fail("Unauthorized execution must not reach the internal service")

    monkeypatch.setattr(task_execution, "request_cancel", deny)
    monkeypatch.setattr(task_execution, "enqueue", deny)
    assert client.post(f"/api/v1/system/executions/{row.id}/cancel").status_code == 404
    assert client.post(f"/api/v1/system/executions/{row.id}/retry").status_code == 404


def test_cancel_is_a_local_persisted_request_and_keeps_run_history(mem_db, execution_api):
    client, user = execution_api
    row = enqueue(mem_db)
    user.tenant_ids = [1]
    response = client.post(f"/api/v1/system/executions/{row.id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["cancel_requested"] is True
    assert mem_db.get(TaskExecutionCancel, row.id) is not None
    assert mem_db.get(InspectionRun, row.run_id) is not None
    assert client.post(f"/api/v1/system/executions/{row.id}/cancel").status_code == 200
    assert mem_db.scalar(select(func.count()).select_from(TaskExecutionCancel)) == 1
    audit = mem_db.scalar(select(AuditLog).where(AuditLog.action == "task.cancel"))
    assert audit.detail["local_only"] is True


@pytest.mark.parametrize("status", ["FAILED", "PARTIAL", "TIMEOUT", "INTERRUPTED", "CANCELLED"])
def test_retry_creates_new_request_in_exact_original_target(mem_db, execution_api, status):
    client, user = execution_api
    row = enqueue(mem_db)
    complete(mem_db, row, status)
    old = (row.id, row.run_id, row.request_id, row.request_key, row.finished_at, list(row.history))
    # Additional allowed projects must not be picked up by a retry.
    mem_db.add(HcsProject(account_id=1, region_id="r2", project_id="other", external_domain="example.invalid"))
    mem_db.commit()
    user.tenant_ids = [1, 2]
    response = client.post(f"/api/v1/system/executions/{row.id}/retry")
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert len(data["execution_ids"]) == 1 and data["execution_ids"][0] != row.id
    retried = mem_db.get(TaskExecution, data["execution_ids"][0])
    assert retried.tenant_id == 1 and retried.project_ref == 11 and retried.status == "QUEUED"
    assert retried.request_id != row.request_id and retried.request_key != row.request_key
    assert retried.run_id != row.run_id and retried.history[0]["retry_of"] == row.id
    assert old == (row.id, row.run_id, row.request_id, row.request_key, row.finished_at, row.history)
    assert row.status == status


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "SUCCESS", "SKIPPED"])
def test_retry_rejects_ineligible_states(mem_db, execution_api, status):
    client, _ = execution_api
    row = enqueue(mem_db)
    row.status = status
    mem_db.commit()
    assert client.post(f"/api/v1/system/executions/{row.id}/retry").status_code == 409
    assert mem_db.scalar(select(func.count()).select_from(TaskExecution)) == 1


def test_targets_explain_disabled_switch_project_and_active_execution(mem_db, execution_api):
    from app.models import CollectSwitch

    client, user = execution_api
    user.tenant_ids = [1]
    active = enqueue(mem_db)
    response = client.get("/api/v1/system/tasks/dcs_status/targets")
    assert response.status_code == 200, response.text
    targets = response.json()["data"]["targets"]
    assert len(targets) == 1 and targets[0]["tenant_id"] == 1
    assert targets[0]["executable"] is False and targets[0]["active_execution_id"] == active.id
    assert "排队" in targets[0]["reason"]
    complete(mem_db, active, "SUCCESS")
    assert client.get("/api/v1/system/tasks/dcs_status/targets").json()["data"]["targets"][0]["executable"]
    mem_db.add(CollectSwitch(tenant_id=1, resource_type="dcs_instance", enabled=False))
    mem_db.commit()
    assert "开关" in client.get("/api/v1/system/tasks/dcs_status/targets").json()["data"]["targets"][0]["reason"]
    mem_db.get(HcsProject, 11).enabled = False
    mem_db.commit()
    assert "项目" in client.get("/api/v1/system/tasks/dcs_status/targets").json()["data"]["targets"][0]["reason"]
    mem_db.get(InspectionTask, "dcs_status").enabled = False
    mem_db.commit()
    assert "任务" in client.get("/api/v1/system/tasks/dcs_status/targets").json()["data"]["targets"][0]["reason"]


def test_cleanup_is_global_and_requires_full_scope_even_with_task_run(mem_db, execution_api):
    client, user = execution_api
    mem_db.add(InspectionTask(code="data_cleanup", name="数据清理", service="platform", interval_sec=86400, enabled=True))
    mem_db.commit()
    user.tenant_ids = [1]
    assert client.post("/api/v1/system/tasks/data_cleanup/run").status_code == 403
    assert client.get("/api/v1/system/tasks/data_cleanup/targets").json()["data"]["targets"] == []
    user.tenant_ids = None
    assert client.post("/api/v1/system/tasks/data_cleanup/run", json={"project_refs": [11]}).status_code == 400
    response = client.post("/api/v1/system/tasks/data_cleanup/run", headers={"X-Tenant-Id": "1"})
    assert response.status_code == 202, response.text
    dto = response.json()["data"]["runs"][0]
    assert dto["tenant_id"] is None and dto["project_ref"] is None
    user.tenant_ids = [1]
    assert client.get(f"/api/v1/system/executions/{dto['id']}").status_code == 404
    assert client.post(f"/api/v1/system/executions/{dto['id']}/cancel").status_code == 404


def test_execution_permissions_distinguish_read_and_run(mem_db, execution_api):
    client, user = execution_api
    row = enqueue(mem_db)
    user.permissions = ["task:view"]
    assert client.get(f"/api/v1/system/executions/{row.id}").status_code == 200
    for path in ("tasks/dcs_status/run", f"executions/{row.id}/cancel", f"executions/{row.id}/retry"):
        assert client.post(f"/api/v1/system/{path}").status_code == 403
    user.permissions = ["task:run"]
    for path in ("executions", f"executions/{row.id}", "tasks/dcs_status/targets", "tasks"):
        assert client.get(f"/api/v1/system/{path}").status_code == 403


@pytest.mark.parametrize("value", [14, 3601, True, "30", 30.5])
def test_timeout_validation_is_strict_and_does_not_change_schedule(mem_db, execution_api, value):
    client, _ = execution_api
    assert client.put("/api/v1/system/tasks/dcs_status", json={"timeout_sec": value}).status_code == 400
    task = mem_db.get(InspectionTask, "dcs_status")
    assert task.interval_sec == 120 and task.params["timeout_sec"] == 60


def test_timeout_patch_uses_parameters_only_and_hides_internal_configuration(mem_db, execution_api):
    client, user = execution_api
    response = client.put("/api/v1/system/tasks/dcs_status", json={"timeout_sec": 15})
    assert response.status_code == 200 and response.json()["data"]["params"] == {"timeout_sec": 15}
    task = mem_db.get(InspectionTask, "dcs_status")
    assert task.interval_sec == 120 and task.params["secret"] == "do-not-expose"
    user.tenant_ids = [1]
    assert client.put("/api/v1/system/tasks/dcs_status", json={"timeout_sec": 50}).status_code == 403
    user.tenant_ids = None
    assert client.put("/api/v1/system/tasks/dcs_status", json={"timeout_sec": None}).json()["data"]["timeout_sec"] == 300
    assert "timeout_sec" not in task.params


def test_mrs_options_are_native_only_validated_and_saved_flat(mem_db, execution_api):
    client, _ = execution_api
    mem_db.add(InspectionTask(code="mrs_job_status", name="原生作业", service="mrs", interval_sec=300, enabled=True,
                              params={"timeout_sec": 90, "secret": "internal", "submitted_time_begin": 1790000000000}))
    mem_db.commit()
    assert client.put("/api/v1/system/tasks/dcs_status", json={"mrs_options": {"page_size": 10}}).status_code == 400
    for options in ({"endpoint": "https://example.invalid"}, {"max_pages": True}, {"page_size": 0},
                    {"query_mode": "active_only", "submitted_window_hours": 24}):
        assert client.put("/api/v1/system/tasks/mrs_job_status", json={"mrs_options": options}).status_code == 400
    response = client.put("/api/v1/system/tasks/mrs_job_status", json={
        "mrs_options": {"query_mode": "active_only", "page_size": 50, "max_pages": 10}})
    assert response.status_code == 200, response.text
    task = mem_db.get(InspectionTask, "mrs_job_status")
    assert task.params["page_size"] == 50 and task.params["query_mode"] == "active_only"
    assert task.params["timeout_sec"] == 90 and task.params["secret"] == "internal"
    assert "mrs_options" not in task.params and "submitted_time_begin" not in task.params
    data = response.json()["data"]
    assert "secret" not in data["params"] and "page_size" in data["parameter_schema"]["properties"]
    assert data["params"] == data["effective_params"]


def test_mrs_invalid_and_null_configuration_cannot_modify_existing_parameters(mem_db, execution_api):
    client, _ = execution_api
    task = InspectionTask(code="mrs_job_status", name="原生作业", service="mrs", interval_sec=300, enabled=True,
                          params={"timeout_sec": 90, "secret": "internal", "query_mode": "active_only", "max_pages": 8})
    mem_db.add(task)
    mem_db.commit()
    original = dict(task.params)
    for options in (None, {"endpoint": "https://example.invalid"}, {"page_size": -1}):
        response = client.put("/api/v1/system/tasks/mrs_job_status", json={"timeout_sec": 70, "mrs_options": options})
        assert response.status_code == 400 and task.params == original
    response = client.put("/api/v1/system/tasks/mrs_job_status", json={"mrs_options": {}})
    assert response.status_code == 200, response.text
    assert task.params["query_mode"] == "active_and_recent" and task.params["submitted_window_hours"] == 24
    assert task.params["timeout_sec"] == 90 and task.params["secret"] == "internal"


def test_running_view_includes_current_progress_counters_before_run_commit(mem_db, execution_api):
    client, _ = execution_api
    row = enqueue(mem_db)
    row.status = "RUNNING"
    row.progress = {"phase": "mrs_executions_page", "api_calls": 7, "resource_total": 4, "abnormal_total": 1,
                    "current": 3, "unit": "page"}
    mem_db.commit()
    data = client.get(f"/api/v1/system/executions/{row.id}").json()["data"]
    assert data["api_calls"] == 7 and data["resource_total"] == 4 and data["abnormal_total"] == 1
    assert data["phase_label"] == "读取 MRS 作业分页" and data["total"] is None


def test_tasks_merge_execution_progress_without_losing_legacy_run_ids(mem_db, execution_api):
    client, user = execution_api
    first = enqueue(mem_db)
    complete(mem_db, first, "FAILED")
    active = enqueue(mem_db)
    enqueue(mem_db, tenant=2, project=21)
    user.tenant_ids = [1]
    response = client.get("/api/v1/system/tasks")
    assert response.status_code == 200, response.text
    task = response.json()["data"][0]
    assert task["latest_execution"]["id"] == active.id
    assert task["active_execution_count"] == 1 and task["queued_count"] == 1
    assert task["params"] == {"timeout_sec": 60} and "do-not-expose" not in response.text
    run = next(run for run in task["runs"] if run["id"] == active.run_id)
    assert run["id"] == run["run_id"] == active.run_id and run["execution_id"] == active.id
    assert run["phase"] == "queued" and run["started_at"] is None
    assert "heartbeat_at" in run and "progress" in run and "history" in run


def test_task_list_queries_do_not_grow_per_task_or_run(mem_db, execution_api):
    client, user = execution_api
    user.tenant_ids = [1]
    enqueue(mem_db)

    def measure():
        queries = []

        def record(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                queries.append(statement)

        engine = mem_db.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            response = client.get("/api/v1/system/tasks")
            assert response.status_code == 200, response.text
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(queries)

    baseline = measure()
    for index in range(15):
        code = f"synthetic_task_{index}"
        mem_db.add(InspectionTask(code=code, name=code, service="dcs", interval_sec=120, enabled=True))
        mem_db.flush()
        for _ in range(3):
            run = enqueue(mem_db, code=code)
            complete(mem_db, run, "SUCCESS")
    assert measure() == baseline


def test_targets_use_local_collection_phase_policy(mem_db, execution_api):
    client, user = execution_api
    user.tenant_ids = [1]
    mem_db.add(InspectionTask(code="ecs_status", name="计算巡检", service="ecs", interval_sec=120, enabled=True))
    mem_db.add(SysSetting(key="collection_policy:1:ecs_server", value={"inspection": {"enabled": False}}))
    mem_db.commit()
    result = client.get("/api/v1/system/tasks/ecs_status/targets").json()["data"]["targets"]
    assert len(result) == 1 and result[0]["executable"] is False and "阶段" in result[0]["reason"]


def test_targets_do_not_expose_disabled_other_tenants(mem_db, execution_api):
    client, user = execution_api
    mem_db.get(HcsAccount, 2).enabled = False
    mem_db.commit()
    user.tenant_ids = [1]
    data = client.get("/api/v1/system/tasks/dcs_status/targets").json()["data"]
    assert {t["tenant_id"] for t in data["targets"]} == {1}
