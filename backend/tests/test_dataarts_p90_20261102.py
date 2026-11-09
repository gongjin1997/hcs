"""P90 来源为上传 PDF 的实例字段；合成数据仅证明计算/隔离，不证明正式局点可用。"""
import math
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.collectors.base import RunContext
from app.collectors.dataarts import DataArtsBatchCollector
from app.hcs import HcsError
from app.models import AlarmEvent, Resource
from app.rules.loader import sync_default_rules
from app.services.pipeline import process_observations

NOW = 1793577600000


def success(identity, minutes, name="job", **extra):
    start = NOW - 180 * 60000
    return {"jobName": name, "instanceId": identity, "planTime": NOW - 3600000 + int(identity) * 1000,
            "submitTime": NOW - 3600000, "status": "success", "startTime": start,
            "endTime": start + int(minutes * 60000), "instanceType": 0, **extra}


def running(name="job"):
    return {"jobName": name, "instanceId": 10000, "planTime": NOW - 1000, "submitTime": NOW - 1000,
            "status": "running", "startTime": NOW - 150 * 60000, "endTime": None}


class Client:
    def __init__(self, workspaces, page_size=3):
        self.workspaces, self.page_size, self.calls = workspaces, page_size, []

    def get(self, service, path, **kwargs):
        assert service == "dataarts"
        rows = self.workspaces[kwargs["headers"]["workspace"]]
        names = list(dict.fromkeys(i["jobName"] for i in rows))
        if path == "/v1/{project_id}/jobs":
            return {"total": len(names), "jobs": [{"name": name, "jobType": "BATCH"} for name in names]}
        if path != "/v1/{project_id}/jobs/instances/detail":
            return {"name": path.rsplit("/", 1)[1], "processType": "BATCH", "nodes": []}
        self.calls.append(deepcopy(kwargs))
        assert kwargs["params"]["minPlanTime"] == NOW - 86400000
        assert kwargs["params"]["maxPlanTime"] == NOW
        workspace = kwargs["headers"]["workspace"]
        rows = self.workspaces[workspace]
        page = kwargs["params"]["offset"]
        start = page * self.page_size
        return {"total": len(rows), "instances": deepcopy(rows[start:start+self.page_size])}


def collect(monkeypatch, rows, **params):
    monkeypatch.setattr("app.collectors.dataarts.time.time", lambda: NOW / 1000)
    client = Client(rows if isinstance(rows, dict) else {"ws": rows})
    context = RunContext(client=client, account="synthetic", project_id="synthetic-project",
                         workspaces=list(client.workspaces), params=params)
    return DataArtsBatchCollector().collect(context), context, client


@pytest.mark.parametrize("values,expected", [([1, 2, 3, 4, 5], 5), (list(range(1, 10)) + [100], 9),
                                             ([0.001, 0.002, 0.003, 0.004, 0.005], 0.005), ([0, 0, 0, 0, 0], 0)])
def test_nearest_rank_uses_unrounded_real_success_durations_and_no_extra_queries(monkeypatch, values, expected):
    instances = [success(i+1, value, executeTime=99999, p90_minutes=99999) for i, value in enumerate(values)]
    rows, context, client = collect(monkeypatch, instances + [running()])
    row = rows[0]
    assert row.facts["p90_minutes"] == row.metrics["p90_minutes"] == expected
    assert row.attrs["batch_baseline"] == {
        "window_start_ms": NOW - 86400000, "window_end_ms": NOW, "sample_count": len(values), "min_samples": 5,
        "method": "NEAREST_RANK_P90", "status": "AVAILABLE", "source": "LOCAL_SUCCESS_INSTANCES"}
    assert len(client.calls) == math.ceil((len(values)+1)/client.page_size)
    assert not context.errors


def test_insufficient_samples_ignore_undocumented_p90_and_execute_time(monkeypatch):
    rows, _, _ = collect(monkeypatch, [success(1, 1), success(2, 2), {**running(), "p90_minutes": 1, "executeTime": 1}])
    assert "p90_minutes" not in rows[0].facts and "p90_minutes" not in rows[0].metrics
    assert rows[0].attrs["batch_baseline"]["sample_count"] == 2
    assert rows[0].attrs["batch_baseline"]["status"] == "INSUFFICIENT_SAMPLES"


@pytest.mark.parametrize("field,value", [("status", "fail"), ("status", "running"), ("status", "running-exception"),
                                         ("status", "pause"), ("status", "manual-stop"), ("status", "SUCCEEDED"),
                                         ("startTime", 0), ("startTime", None), ("startTime", True),
                                         ("startTime", float("nan")), ("startTime", "bad"),
                                         ("endTime", None), ("endTime", 0), ("endTime", True), ("endTime", NOW-181*60000)])
def test_non_success_or_invalid_millisecond_intervals_never_fill_minimum_samples(monkeypatch, field, value):
    rows, _, _ = collect(monkeypatch, [success(i, 1) for i in range(1, 5)] + [success(5, 999, **{field: value}), running()])
    assert rows[0].attrs["batch_baseline"]["sample_count"] == 4
    assert "p90_minutes" not in rows[0].facts and "p90_minutes" not in rows[0].metrics


def test_workspace_job_and_unique_execution_identity_are_not_mixed(monkeypatch):
    spaces = {"a": [success(i, i) for i in range(1, 6)] + [running()],
              "b": [success(i, 100+i) for i in range(1, 5)] + [running(),
                    *[success(i, 99, name="other") for i in range(1, 6)]]}
    rows, _, _ = collect(monkeypatch, spaces)
    by_id = {row.resource_id: row for row in rows}
    assert by_id["a/job"].facts["p90_minutes"] == 5
    assert "p90_minutes" not in by_id["b/job"].facts
    assert by_id["b/job"].attrs["batch_baseline"]["sample_count"] == 4


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 2.5, "5"])
def test_invalid_local_minimum_policy_fails_before_cloud_request(monkeypatch, value):
    client = Client({"ws": [running()]})
    context = RunContext(client=client, account="synthetic", project_id="synthetic", workspaces=["ws"],
                         params={"p90_min_samples": value})
    with pytest.raises(HcsError):
        DataArtsBatchCollector().collect(context)
    assert client.calls == []


def test_explicit_positive_minimum_is_a_local_policy_not_fixed_baseline(monkeypatch):
    rows, _, _ = collect(monkeypatch, [success(1, 7), success(2, 3), running()], p90_min_samples=2)
    assert rows[0].facts["p90_minutes"] == 7 and rows[0].attrs["batch_baseline"]["min_samples"] == 2


def test_page_total_drift_never_builds_a_partial_baseline(monkeypatch):
    monkeypatch.setattr("app.collectors.dataarts.time.time", lambda: NOW / 1000)

    class Drifting(Client):
        def get(self, service, path, **kwargs):
            page = kwargs["params"]["offset"]
            return {"total": 6 if page == 0 else 4, "instances": [success(page*2+1, 1), success(page*2+2, 1)]}

    context = RunContext(client=Drifting({}), account="synthetic", project_id="synthetic", workspaces=["ws"],
                         params={"p90_min_samples": 3})
    assert DataArtsBatchCollector().collect(context) == [] and context.errors


def test_real_derived_baseline_makes_existing_timeout_rule_effective(mem_db, monkeypatch):
    rows, context, _ = collect(monkeypatch, [success(i, i) for i in range(1, 6)] + [running()])
    sync_default_rules(mem_db)
    context.params["_task_code"] = "dgc_batch_instance"
    process_observations(mem_db, "dataarts", rows, context, datetime.fromtimestamp(NOW/1000, UTC))
    alarms = mem_db.scalars(select(AlarmEvent)).all()
    assert any(alarm.rule_id == "R-DGC-BAT-003" and alarm.status == "FIRING" for alarm in alarms)


def test_measured_p90_and_window_are_consistent_in_job_report_and_excel(mem_db, monkeypatch):
    from io import BytesIO

    from openpyxl import load_workbook

    from app.core.deps import CurrentUser, Scope
    from app.reports.job_excel import job_workbook
    from app.services import job_report_service

    rows, context, _ = collect(monkeypatch, [success(i, i) for i in range(1, 6)] + [running()])
    stamp = datetime.fromtimestamp(NOW/1000, UTC)
    context.params["_task_code"] = "dgc_batch_instance"
    process_observations(mem_db, "dataarts", rows, context, stamp)
    scope = Scope(None, True, CurrentUser(id=1, username="synthetic", display_name="synthetic"))
    monkeypatch.setattr(job_report_service, "utcnow", lambda: stamp)
    report = job_report_service.get_job_report(mem_db, scope, "batch", job_report_service.JobReportFilters())
    assert report.items[0].metrics["p90_minutes"] == 5
    assert report.items[0].status_details["batch_baseline"]["sample_count"] == 5
    content = job_workbook(mem_db, scope, "batch", job_report_service.JobReportFilters())
    ws = load_workbook(BytesIO(content))["作业明细"]
    row = dict(zip((cell.value for cell in ws[1]), next(ws.iter_rows(min_row=2, values_only=True)), strict=True))
    assert row["历史 P90（分钟）"] == 5 and row["基线有效样本数"] == row["基线最低样本数"] == 5
    assert isinstance(row["基线窗口开始"], datetime) and isinstance(row["基线窗口结束"], datetime)
    assert row["基线状态"] == "AVAILABLE"


def test_losing_baseline_samples_never_recovers_an_existing_timeout(mem_db, monkeypatch):
    sync_default_rules(mem_db)
    first, context, _ = collect(monkeypatch, [success(i, i) for i in range(1, 6)] + [running()])
    stamp = datetime.fromtimestamp(NOW/1000, UTC)
    context.params["_task_code"] = "dgc_batch_instance"
    process_observations(mem_db, "dataarts", first, context, stamp)
    second, _, _ = collect(monkeypatch, [success(i, i) for i in range(1, 5)] + [running()])
    process_observations(mem_db, "dataarts", second, context, stamp)
    alarm = mem_db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-DGC-BAT-003"))
    assert alarm.status == "FIRING"
    assert mem_db.scalar(select(Resource)).metrics["p90_minutes"] is None
    assert "p90_minutes" not in second[0].facts and second[0].attrs["batch_baseline"]["sample_count"] == 4


def test_duplicate_instance_identity_never_inflates_the_success_sample_count(monkeypatch):
    rows, context, _ = collect(monkeypatch, [success(i, i) for i in range(1, 5)] + [success("1", 99), running()])
    assert rows and all(row.attrs["instance_status"] is None and "p90_minutes" not in row.facts for row in rows)
    assert context.errors


def test_partial_workspace_does_not_supply_baseline_or_block_other_workspace(monkeypatch):
    monkeypatch.setattr("app.collectors.dataarts.time.time", lambda: NOW / 1000)
    values = [success(i, i) for i in range(1, 6)] + [running()]

    class Partial(Client):
        def get(self, service, path, **kwargs):
            if path.endswith("/instances/detail") and kwargs["headers"]["workspace"] == "a" and kwargs["params"]["offset"] == 1:
                raise HcsError(503, "SYNTHETIC", "local injected page failure")
            return super().get(service, path, **kwargs)

    context = RunContext(client=Partial({"a": values, "b": values}), account="synthetic", project_id="synthetic",
                         workspaces=["a", "b"])
    rows = DataArtsBatchCollector().collect(context)
    assert [row.resource_id for row in rows] == ["a/job", "b/job"]
    assert rows[0].facts == {} and rows[0].attrs["instance_query_status"] == "FAILED"
    assert rows[1].facts["p90_minutes"] == 5
    assert context.errors and rows[1].attrs["batch_baseline"]["sample_count"] == 5
