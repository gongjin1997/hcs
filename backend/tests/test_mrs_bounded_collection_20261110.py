"""MRS 范围/预算合成回归；不访问云端。"""
import logging
from copy import deepcopy

import pytest

from app.collectors.base import RunContext
from app.collectors.mrs_native import MrsJobCollector
from app.collectors.mrs_params import mrs_parameter_schema, native_queries, validate_mrs_params
from app.hcs import HcsError
from app.services.collect_scope import Scopes

NOW = 1_793_952_000_000
ACTIVE = ("NEW", "NEW_SAVING", "SUBMITTED", "ACCEPTED", "RUNNING")


def execution(jid, state="FINISHED", age_hours=1):
    return {"job_id": jid, "job_name": "same", "job_state": state,
            "job_result": "UNDEFINED" if state in ACTIVE else "SUCCEEDED",
            "submitted_time": NOW - age_hours * 3_600_000}


class Client:
    def __init__(self, rows=(), *, clusters=("c1",), ignore=None):
        self.rows, self.clusters, self.ignore, self.calls = list(rows), clusters, ignore, []

    def get(self, service, path, *, params):
        assert service == "mrs"
        self.calls.append((path, deepcopy(params)))
        if path.endswith("cluster_infos"):
            start = (params["currentPage"] - 1) * params["pageSize"]
            rows = [{"clusterId": c, "clusterName": c} for c in self.clusters]
            return {"clusterTotal": len(rows), "clusters": rows[start:start + params["pageSize"]]}
        rows = self.rows
        if "job_state" in params and self.ignore != "state":
            rows = [r for r in rows if r["job_state"] == params["job_state"]]
        if "submitted_time_begin" in params and self.ignore != "time":
            rows = [r for r in rows if r["submitted_time"] >= params["submitted_time_begin"]]
        if "submitted_time_end" in params and self.ignore != "time":
            rows = [r for r in rows if r["submitted_time"] <= params["submitted_time_end"]]
        start = (params["offset"] - 1) * params["limit"]
        return {"total_record": len(rows), "job_list": deepcopy(rows[start:start + params["limit"]])}


def context(client, **params):
    return RunContext(client=client, account="synthetic", project_id="synthetic", params=params)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr("app.collectors.mrs_native.time.time", lambda: NOW / 1000)


def test_default_active_and_recent_preserves_year_old_flink_and_deduplicates():
    client = Client([execution("long", "RUNNING", 365 * 24), execution("active", "ACCEPTED"),
                     execution("recent"), execution("old", age_hours=48)])
    ctx = context(client)
    rows = MrsJobCollector().collect(ctx)
    assert {r.resource_id for r in rows} == {"c1/long", "c1/active", "c1/recent"}
    queries = [p for path, p in client.calls if "job-executions" in path]
    assert [p.get("job_state") for p in queries] == [*ACTIVE, None]
    assert all("submitted_time_begin" not in p for p in queries[:5])
    assert queries[-1]["submitted_time_begin"] == NOW - 24 * 3_600_000
    assert ctx.stats["mrs_jobs"]["duplicates"] == 1
    assert ctx.stats["mrs_jobs"]["coverage_complete"] and not ctx.errors


@pytest.mark.parametrize("ignored", ["state", "time"])
def test_ignored_filter_is_unconfirmed_coverage_without_history_fallback(ignored):
    client = Client([execution("old", age_hours=48)], ignore=ignored)
    ctx = context(client)
    MrsJobCollector().collect(ctx)
    assert ctx.errors and not ctx.stats["mrs_jobs"]["coverage_complete"]
    assert ctx.stats["mrs_jobs"]["coverage"] == "unconfirmed"
    assert all("job_state" in p or "submitted_time_begin" in p
               for path, p in client.calls if "job-executions" in path)


def test_max_calls_includes_discovery_and_stops_before_next_call():
    client = Client(clusters=("c1", "c2"))
    ctx = context(client, max_calls=2)
    MrsJobCollector().collect(ctx)
    assert len(client.calls) == 2 and ctx.errors
    assert ctx.stats["mrs_jobs"]["calls"] == 2
    assert not ctx.stats["mrs_jobs"]["coverage_complete"]


def test_cluster_discovery_reports_actual_page_total():
    client = Client(clusters=tuple(f"c{i}" for i in range(51)))
    ctx = context(client, max_calls=2)
    updates = []
    ctx.progress = lambda phase, **details: updates.append((phase, details))

    MrsJobCollector().collect(ctx)

    pages = [details for phase, details in updates if phase == "mrs_clusters" and details.get("total") is not None]
    assert [(item["current"], item["total"]) for item in pages] == [(1, 2), (2, 2)]


def test_max_pages_is_shared_across_queries_and_clusters():
    client = Client(clusters=("c1", "c2"))
    ctx = context(client, max_pages=2)
    MrsJobCollector().collect(ctx)
    assert sum("job-executions" in path for path, _ in client.calls) == 2
    assert ctx.errors and not ctx.stats["mrs_jobs"]["coverage_complete"]


@pytest.mark.parametrize("state", ACTIVE)
def test_each_active_state_is_collected_without_time_window(state):
    client = Client([execution("long", state, age_hours=9000)])
    ctx = context(client)
    rows = MrsJobCollector().collect(ctx)
    assert len(rows) == 1 and rows[0].status_raw == state and not ctx.errors


@pytest.mark.parametrize("mode,ids,count", [
    ("active_only", {"c1/active"}, 5), ("recent_only", {"c1/recent"}, 1),
    ("all_history", {"c1/active", "c1/recent", "c1/old"}, 1),
])
def test_explicit_modes_do_not_silently_expand_or_combine(mode, ids, count):
    client = Client([execution("active", "RUNNING", 48), execution("recent"), execution("old", age_hours=48)])
    ctx = context(client, query_mode=mode)
    assert {r.resource_id for r in MrsJobCollector().collect(ctx)} == ids
    queries = [p for path, p in client.calls if "job-executions" in path]
    assert len(queries) == count and not ctx.errors
    assert ctx.stats["mrs_jobs"]["coverage_complete"]
    if mode == "active_only":
        assert all("submitted_time_begin" not in p for p in queries)
    if mode == "all_history":
        assert all(set(p) == {"offset", "limit"} for p in queries)


def test_recent_window_is_fixed_across_pages_and_only_changes_recent_partition():
    client = Client([execution("1", age_hours=2), execution("2", age_hours=4), execution("old", age_hours=10)])
    ctx = context(client, page_size=1, submitted_window_hours=6)
    assert {r.resource_id for r in MrsJobCollector().collect(ctx)} == {"c1/1", "c1/2"}
    recent = [p for _, p in client.calls if "submitted_time_begin" in p]
    assert len(recent) == 2 and {p["submitted_time_begin"] for p in recent} == {NOW - 6 * 3_600_000}
    assert {p["submitted_time_end"] for p in recent} == {NOW} and not ctx.errors


def test_explicit_absolute_window_does_not_apply_to_activity():
    begin, end = NOW - 4 * 3_600_000, NOW - 2 * 3_600_000
    client = Client([execution("long", "RUNNING", 100), execution("recent", age_hours=3), execution("new")])
    ctx = context(client, submitted_time_begin=begin, submitted_time_end=end)
    assert {r.resource_id for r in MrsJobCollector().collect(ctx)} == {"c1/long", "c1/recent"}
    assert client.calls[-1][1]["submitted_time_end"] == end and not ctx.errors


@pytest.mark.parametrize("params", [
    {"query_mode": "automatic"}, {"max_calls": True}, {"max_calls": 0}, {"max_calls": 401},
    {"max_items": "10"}, {"max_pages": 201}, {"submitted_window_hours": float("inf")},
    {"submitted_window_hours": float("nan")}, {"submitted_window_hours": 10**1000},
    {"submitted_window_hours": "24"}, {"submitted_time_begin": True},
    {"submitted_time_begin": NOW // 1000}, {"submitted_time_end": NOW},
    {"submitted_time_begin": NOW, "submitted_time_end": NOW - 1},
    {"submitted_time_begin": NOW, "submitted_window_hours": 12},
    {"submitted_time_begin": NOW + 1},
    {"query_mode": "active_only", "submitted_window_hours": 12},
    {"query_mode": "all_history", "submitted_time_begin": NOW},
])
def test_invalid_params_are_rejected_before_discovery(params):
    client = Client()
    with pytest.raises(HcsError) as exc:
        MrsJobCollector().collect(context(client, **params))
    assert exc.value.code == "LOCAL.INVALID_CONFIG" and client.calls == []


def test_schema_is_finite_copy_and_api_unknown_fields_are_rejected():
    schema = mrs_parameter_schema("mrs_job_status")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["query_mode"]["default"] == "active_and_recent"
    schema["properties"]["query_mode"]["default"] = "all_history"
    assert mrs_parameter_schema("mrs_job_status")["properties"]["query_mode"]["default"] == "active_and_recent"
    for key in ("endpoint", "headers", "window_hours", "arbitrary_filter"):
        with pytest.raises(HcsError):
            validate_mrs_params("mrs_job_status", {key: "invalid"})


@pytest.mark.parametrize("mode", ["active_and_recent", "active_only", "recent_only", "all_history"])
def test_normalized_config_can_be_saved_and_validated_again(mode):
    config = validate_mrs_params("mrs_job_status", {"query_mode": mode})
    assert validate_mrs_params("mrs_job_status", config) == config


def test_api_full_replacement_removes_old_native_windows_and_preserves_execution_options():
    fields = set(mrs_parameter_schema("mrs_job_status")["properties"])
    saved = {"query_mode": "recent_only", "submitted_time_begin": NOW - 3_600_000,
             "submitted_time_end": NOW, "page_size": 7, "max_calls": 9,
             "timeout_sec": 321, "_internal": {"version": 2}, "window_hours": 48}

    def replace(options):
        nonlocal saved
        normalized = validate_mrs_params("mrs_job_status", options, strict=True)
        saved = {**{key: value for key, value in saved.items() if key not in fields}, **normalized}
        public = {key: value for key, value in saved.items() if key in fields}
        assert validate_mrs_params("mrs_job_status", public, strict=True) == normalized
        return public

    public = replace({"query_mode": "all_history"})
    assert public["page_size"] == 100 and public["max_calls"] == 400
    assert not {"submitted_time_begin", "submitted_time_end", "submitted_window_hours"} & public.keys()
    assert saved["timeout_sec"] == 321 and saved["_internal"] == {"version": 2} and saved["window_hours"] == 48
    assert "timeout_sec" not in public and "_internal" not in public and "window_hours" not in public
    public = replace({})
    assert public["query_mode"] == "active_and_recent" and public["submitted_window_hours"] == 24
    assert "submitted_time_begin" not in public and "submitted_time_end" not in public


def test_legacy_node_window_does_not_change_native_default_window():
    client = Client([execution("long", "RUNNING", 100)])
    ctx = context(client, window_hours=1, max_details=1, _task_code="mrs_job_status")
    assert len(MrsJobCollector().collect(ctx)) == 1 and not ctx.errors
    assert client.calls[-1][1]["submitted_time_begin"] == NOW - 24 * 3_600_000
    assert ctx.stats["mrs_jobs"]["legacy_window_hours_ignored"]


def test_budget_counts_duplicate_rows_across_queries_and_does_not_publish_partial_as_complete():
    client = Client([execution("a", "NEW"), execution("b", "RUNNING")])
    ctx = context(client, max_items=2)
    rows = MrsJobCollector().collect(ctx)
    assert {r.resource_id for r in rows} == {"c1/a", "c1/b"}
    assert ctx.errors and ctx.stats["mrs_jobs"]["items_returned"] == 2
    assert not ctx.stats["mrs_jobs"]["coverage_complete"]
    assert len(client.calls) == 6  # 清单+五个活动查询，预算不允许再猜近期为空。
    assert ctx.stats["mrs_jobs"]["budget_exceeded"] == {
        "budget_key": "max_items", "budget_limit": 2, "budget_used": 2, "cluster_id": "c1", "query": "recent",
    }
    assert "本查询总数" not in ctx.errors[0]  # 未请求近期查询，不能虚构其总数。


def test_default_budget_completes_large_recent_queries_across_clusters():
    client = Client([execution("long", "RUNNING", 9000), execution("active", "ACCEPTED"),
                     *(execution(f"recent-{i}") for i in range(2500))], clusters=("c1", "c2"))
    ctx = context(client)

    rows = MrsJobCollector().collect(ctx)

    assert len(rows) == 5004 and not ctx.errors
    assert {"c1/long", "c2/long", "c1/recent-2499", "c2/recent-2499"} <= {r.resource_id for r in rows}
    stats = ctx.stats["mrs_jobs"]
    assert stats["coverage_complete"] and stats["clusters_completed"] == 2
    assert stats["items_returned"] == 5006 and stats["duplicates"] == 2
    assert stats["pages"] == 62 and stats["calls"] == len(client.calls) == 63
    assert stats["queries_completed"] == stats["queries_planned"] == 12
    assert stats["budgets"] == {"max_calls": 400, "max_pages": 200, "max_items": 20000,
                                "max_clusters": 1000, "page_size": 100}


def test_default_budget_can_read_twenty_thousand_recent_rows_without_changing_page_size():
    client = Client(execution(str(i)) for i in range(20000))
    ctx = context(client, query_mode="recent_only")

    assert len(MrsJobCollector().collect(ctx)) == 20000 and not ctx.errors
    assert ctx.stats["mrs_jobs"]["coverage_complete"]
    assert ctx.stats["mrs_jobs"]["pages"] == 200
    assert len(client.calls) == 201
    assert {p["limit"] for path, p in client.calls if "job-executions" in path} == {100}


def test_item_budget_error_reports_insufficient_capacity_before_limit_is_used(caplog):
    client = Client(execution(str(i)) for i in range(2501))
    ctx = context(client, query_mode="recent_only", max_items=2000)
    caplog.set_level(logging.WARNING, logger="app.collectors.mrs_native")

    assert MrsJobCollector().collect(ctx) == []

    assert len(client.calls) == 2 and not ctx.stats["mrs_jobs"]["coverage_complete"]
    error = ctx.errors[0]
    assert "max_items" in error and "上限=2000" in error and "已用=100" in error
    assert "本查询总数=2501" in error and "本轮至少需要=2501" in error
    assert "巡检任务" in error and "预算耗尽" not in error
    record = next(r for r in caplog.records if r.msg == "mrs_budget_exceeded")
    assert record.cluster_id == "c1" and record.query == "recent"
    assert record.budget_key == "max_items" and record.budget_limit == 2000 and record.budget_used == 100
    assert record.query_total == record.required_total == 2501
    assert record.calls == 2 and record.pages == 1 and record.items_returned == 100


def test_item_budget_requirement_includes_previous_clusters_but_not_current_page_twice():
    client = Client((execution(str(i)) for i in range(1200)), clusters=("c1", "c2"))
    ctx = context(client, query_mode="recent_only", max_items=2000)

    rows = MrsJobCollector().collect(ctx)

    assert len(rows) == 1200 and all(r.resource_id.startswith("c1/") for r in rows)
    stats = ctx.stats["mrs_jobs"]
    assert stats["clusters_completed"] == 1 and not stats["coverage_complete"]
    assert stats["items_returned"] == 1300 and len(client.calls) == 14
    assert stats["budget_exceeded"] == {
        "budget_key": "max_items", "budget_limit": 2000, "budget_used": 1300,
        "query_total": 1200, "required_total": 2400, "cluster_id": "c2", "query": "recent",
    }


def test_exact_item_budget_on_last_query_can_complete_and_keeps_page_size_stable():
    client = Client([execution(str(i)) for i in range(3)])
    ctx = context(client, query_mode="recent_only", page_size=2, max_items=3)
    assert len(MrsJobCollector().collect(ctx)) == 3 and not ctx.errors
    assert [p["limit"] for path, p in client.calls if "job-executions" in path] == [2, 2]
    assert ctx.stats["mrs_jobs"]["coverage_complete"]


def test_failed_calls_are_not_free_and_cannot_reset_budget_for_next_cluster():
    class Failed(Client):
        def get(self, service, path, *, params):
            result = super().get(service, path, params=params)
            if "job-executions" in path:
                raise HcsError(503, "SYNTHETIC.FAILURE", "unavailable")
            return result
    client = Failed(clusters=("c1", "c2"))
    ctx = context(client, max_calls=3)
    assert MrsJobCollector().collect(ctx) == []
    assert len(client.calls) == ctx.stats["mrs_jobs"]["calls"] == 3
    assert ctx.errors and not ctx.stats["mrs_jobs"]["coverage_complete"]
    assert ctx.stats["mrs_jobs"]["budget_exceeded"]["budget_key"] == "max_calls"
    assert ctx.stats["mrs_jobs"]["budget_exceeded"]["budget_used"] == 3


def test_client_retry_attempts_share_round_call_budget_and_do_not_get_fresh_allowance():
    from app.hcs.ratelimit import consume_budget

    class Retrying(Client):
        wire_attempts = 0

        def get(self, service, path, *, params):
            result = super().get(service, path, params=params)
            for _ in range(2 if "job-executions" in path else 1):
                consume_budget()
                self.wire_attempts += 1
            return result
    client = Retrying()
    ctx = context(client, max_calls=3)
    assert MrsJobCollector().collect(ctx) == [] and ctx.errors
    assert client.wire_attempts == 3 and ctx.stats["mrs_jobs"]["calls"] <= 3
    assert not ctx.stats["mrs_jobs"]["coverage_complete"]
    assert ctx.stats["mrs_jobs"]["budget_exceeded"]["budget_key"] == "upstream_request_budget"
    assert "budget_used" not in ctx.stats["mrs_jobs"]["budget_exceeded"]


def test_cluster_discovery_pagination_is_charged_and_cannot_return_partial_inventory():
    client = Client(clusters=tuple(f"c{i}" for i in range(51)))
    ctx = context(client, max_calls=1)
    assert MrsJobCollector().collect(ctx) == []
    assert len(client.calls) == 1 and ctx.errors
    assert not ctx.stats["mrs_jobs"]["coverage_complete"]


def test_scope_cuts_clusters_before_any_execution_call_even_after_inventory_pagination():
    client = Client([execution("a")], clusters=tuple(f"c{i}" for i in range(51)))
    ctx = context(client)
    ctx.tenant_id = 11
    ctx.scopes = Scopes(raw={(11, "mrs_job"): {"rules": [
        {"effect": "include", "match": {"cluster": ["c50"]}}]}})
    rows = MrsJobCollector().collect(ctx)
    assert {r.resource_id for r in rows} == {"c50/a"} and not ctx.errors
    queries = [path for path, _ in client.calls if "job-executions" in path]
    assert len(queries) == 6 and all("/c50/" in path for path in queries)


def test_deduplication_keeps_same_job_id_in_two_clusters():
    client = Client([execution("shared", "RUNNING")], clusters=("c1", "c2"))
    ctx = context(client)
    assert {r.resource_id for r in MrsJobCollector().collect(ctx)} == {"c1/shared", "c2/shared"}
    assert ctx.stats["mrs_jobs"]["duplicates"] == 2 and not ctx.errors


@pytest.mark.parametrize("submitted", [None, True, "unknown", NOW // 1000, NOW - 48 * 3_600_000, NOW + 1])
def test_missing_or_outside_submitted_time_cannot_prove_window_coverage(submitted):
    row = execution("a")
    row["submitted_time"] = submitted
    client = Client([row], ignore="time")
    ctx = context(client, query_mode="recent_only")
    assert MrsJobCollector().collect(ctx) == [] and ctx.errors
    assert ctx.stats["mrs_jobs"]["coverage"] == "unconfirmed"


def test_progress_uses_actual_counts_and_deadline_exceptions_propagate(caplog):
    ctx = context(Client([execution("a")]), query_mode="recent_only")
    updates = []
    ctx.progress = lambda phase, **details: updates.append((phase, details))
    caplog.set_level(logging.INFO, logger="app.collectors.mrs_native")
    assert len(MrsJobCollector().collect(ctx)) == 1
    assert {p for p, _ in updates} >= {"mrs_clusters", "mrs_executions_page", "mrs_executions_process"}
    assert any(p == "mrs_executions_page" and d["current"] == d["total"] == 1 for p, d in updates)
    assert any(p == "mrs_executions_page" and d.get("cluster_id") == "c1" and d.get("query") == "recent"
               for p, d in updates)
    assert all("percent" not in d and "percentage" not in d for _, d in updates)
    assert any(p == "mrs_executions_process" and d["current"] == d["total"] == 1 for p, d in updates)
    request_logs = [record for record in caplog.records if record.msg == "job_collection_page"]
    assert len(request_logs) == 1 and request_logs[0].cost_ms >= 0
    cluster_logs = [record for record in caplog.records if record.msg == "mrs_api_page_completed"]
    assert len(cluster_logs) == 1 and cluster_logs[0].phase == "cluster_discovery" and cluster_logs[0].cost_ms >= 0
    client = Client()
    ctx = context(client)

    def deadline(phase, **details):
        if phase == "mrs_executions_page":
            raise TimeoutError("synthetic executor deadline")
    ctx.progress = deadline
    with pytest.raises(TimeoutError):
        MrsJobCollector().collect(ctx)
    assert len(client.calls) == 1


def test_failed_mrs_page_log_includes_query_context_and_elapsed_time(caplog):
    client = Client()
    get = client.get

    def fail_job_query(service, path, *, params):
        if "job-executions" in path:
            raise HcsError(504, "HTTP.504", "synthetic upstream timeout")
        return get(service, path, params=params)

    client.get = fail_job_query
    ctx = context(client, query_mode="recent_only")
    caplog.set_level(logging.WARNING, logger="app.collectors.mrs_native")

    assert MrsJobCollector().collect(ctx) == []

    failure = next(record for record in caplog.records if record.msg == "mrs_api_page_failed")
    assert failure.phase == "job_executions" and failure.cluster_id == "c1" and failure.query == "recent"
    assert failure.request_attempted is True and failure.cost_ms >= 0
    assert failure.error_code == "HTTP.504" and failure.getMessage() == "mrs_api_page_failed"


def test_unmanaged_runcontext_progress_keeps_local_real_statistics():
    ctx = context(Client([execution("a")]), query_mode="recent_only")
    assert len(MrsJobCollector().collect(ctx)) == 1
    last = ctx.stats["mrs_progress"]
    assert last["phase"] == "mrs_collection_complete" and last["executions"] == 1
    assert last["current"] == last["total"] == 1 and last["coverage"] == "response_validated"


def test_dataarts_schema_keeps_independent_original_defaults():
    config = validate_mrs_params("dgc_mrs_node_status", {})
    assert config == {"window_hours": 24, "max_details": 500, "hcs_concurrency": 4}
    assert native_queries(validate_mrs_params("mrs_job_status", {}), NOW)[-1][1]["submitted_time_begin"] == NOW - 86400000


def test_dataarts_existing_detail_pool_reports_bounded_batches_and_preserves_window():
    from app.collectors.mrs import DataArtsMrsNodeCollector

    from .test_mrs_hcs851_contract import DataArtsClient, inst, job_ctx, node

    instances = [inst(name=f"job{i}") for i in range(5)]
    client = DataArtsClient(instances, {(f"job{i}", 1): [node()] for i in range(5)})
    ctx = job_ctx(client, window_hours=48, hcs_concurrency=2)
    updates = []
    ctx.progress = lambda phase, **details: updates.append((phase, details))
    assert len(DataArtsMrsNodeCollector().collect(ctx)) == 5 and not ctx.errors
    assert client.requests[0][2]["params"]["maxPlanTime"] - client.requests[0][2]["params"]["minPlanTime"] == 48 * 3_600_000
    assert [d["current"] for p, d in updates if p == "dataarts_mrs_details"] == [0, 2, 2, 4, 4, 5]
