"""后台日志：上下文、失败栈、无敏感正文、空轮询安静以及稳定错峰/重叠抑制。"""
import json
import logging
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DataError

from app.collectors import dataarts, ecs, mrs
from app.collectors.base import Observation, RunContext
from app.core import events
from app.core.config import get_settings
from app.core.logging import JsonFormatter, log_context
from app.core.response import BizError
from app.hcs import HcsError
from app.ledger import sync as ledger_sync
from app.models import AlarmEvent, Contact, InspectionRun, Notification, User
from app.notify import dispatcher, smtp
from app.reports import service as reports
from app.rules import evaluator
from app.rules.model import Rule
from app.services import mail_settings_service, pipeline
from app.services.tenancy import Target
from app.workers import bootstrap, notifier, run_once, scheduler

TARGET = Target(101, "logging", "logging", "logging", 201, "test-project", "project", "region")


@contextmanager
def _capture_business_logs():
    rows = []

    class Capture(logging.Handler):
        def emit(self, record):
            rows.append(json.loads(JsonFormatter("test-worker").format(record)))

    root = logging.getLogger()
    handler = Capture()
    names = ["app.services.pipeline", "scheduler", "app.core.events", "app.notify.dispatcher", "app.notify.smtp",
             "bootstrap", "notifier", "app.reports.service", "app.rules.evaluator", "app.collectors.dataarts",
             "app.collectors.mrs", "app.collectors.ecs", "app.ledger.sync", "app.workers.run_once"]
    states = {name: (logging.getLogger(name).level, logging.getLogger(name).disabled) for name in names}
    for name in names:
        logger = logging.getLogger(name)
        logger.disabled = False  # Alembic fileConfig 默认会禁用此前已经创建的业务 logger。
        logger.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        yield rows
    finally:
        root.removeHandler(handler)
        handler.close()
        for name, (level, disabled) in states.items():
            logger = logging.getLogger(name)
            logger.setLevel(level)
            logger.disabled = disabled


@pytest.fixture
def logged():
    with _capture_business_logs() as rows:
        yield rows


@pytest.fixture
def delivery_recipient(mem_db):
    user = User(username="logging-recipient", display_name="合成日志收件人", email="review@example.invalid",
                password_hash="!", login_enabled=False, enabled=True, notify_enabled=True, tenant_ids=[101])
    mem_db.add(user)
    mem_db.flush()
    mem_db.add(Contact(user_id=user.id, name=user.display_name, email=user.email, enabled=True))
    mem_db.commit()
    return user


def test_capture_reenables_disabled_loggers_and_restores_their_state():
    loggers = [logging.getLogger(name) for name in ("app.services.pipeline", "app.workers.run_once")]
    prior = [(logger.level, logger.disabled) for logger in loggers]
    root = logging.getLogger()
    handlers, root_level = list(root.handlers), root.level
    try:
        for logger in loggers:
            logger.setLevel(logging.ERROR)
            logger.disabled = True
        with _capture_business_logs() as rows, log_context(run_id=91):
            for logger in loggers:
                logger.info("capture_state_regression", extra={"event": "capture_state_regression"})
        assert len(rows) == 2 and all(row["run_id"] == 91 for row in rows)
        assert {row["logger"] for row in rows} == {logger.name for logger in loggers}
        assert all(logger.disabled and logger.level == logging.ERROR for logger in loggers)
        assert root.handlers == handlers and root.level == root_level
    finally:
        for logger, (level, disabled) in zip(loggers, prior, strict=True):
            logger.setLevel(level)
            logger.disabled = disabled


class Client:
    calls = 0

    def configure_qps(self, settings):
        pass

    def metrics_snapshot(self):
        return {"calls": self.calls, "errors": 0, "rate_limit_ms": 0, "services": {}}


@pytest.mark.parametrize("status", ["SUCCESS", "PARTIAL", "FAILED"])
def test_task_lifecycle_has_run_scope_counts_and_safe_trace(mem_db, monkeypatch, logged, status):
    client = Client()
    error = HcsError(503, "TEST.UNAVAILABLE", "unstructured-private-response")

    def collect(ctx):
        client.calls += 1
        if status == "FAILED":
            raise error
        if status == "PARTIAL":
            ctx.errors.append("unstructured-private-response")
        return [Observation("ecs_server", "one", "one", "cpu", {"cpu_util": 10})]

    monkeypatch.setitem(pipeline.TASKS, "logging-test", SimpleNamespace(service="ecs", collect=collect))
    monkeypatch.setattr(pipeline, "process_observations", lambda *a, **kw: ([], 0))
    monkeypatch.setattr(pipeline.platform_watch, "on_run", lambda *a: None)
    with log_context(request_id="parent-request", run_id="outer"):
        run = pipeline.run_task(mem_db, "logging-test", client=client, target=TARGET)
        logging.getLogger("app.services.pipeline").debug("outside")
    assert run.status == status
    starts = [row for row in logged if row.get("event") == "task_started"]
    ends = [row for row in logged if row.get("event") == "task_finished"]
    assert len(starts) == len(ends) == 1
    for row in starts + ends:
        assert row["run_id"] == run.id and row["tenant_id"] == 101 and row["project_id"] == "test-project"
        assert row["cloud_service"] == "ecs" and row["service"] == "test-worker"
        assert row["request_id"] == "parent-request"
        assert row["task_execution_id"] == starts[0]["task_execution_id"]
    assert ends[0]["api_calls"] == 1 and ends[0]["cost_ms"] >= 0
    assert next(row for row in logged if row["msg"] == "outside")["run_id"] == "outer"
    if status == "FAILED":
        failure = next(row for row in logged if row.get("event") == "task_collection_failed")
        assert failure["exception_type"] == "HcsError" and "Traceback" in failure["exc"]
    assert "unstructured-private-response" not in json.dumps(logged)


def test_ledger_database_failure_logs_stage_counts_and_safe_diagnostic(mem_db, monkeypatch, logged):
    class LedgerClient(Client):
        project_id = "test-project"
        tenant_id = 101
        project_ref = 201

        def get(self, service, path, **kwargs):
            self.calls += 1
            assert (service, path) == ("dcs", "/v2/{project_id}/instances")
            return {"instances": [{"instance_id": "instance-1", "name": "cache-1", "status": "RUNNING",
                                    "engine": "Redis", "engine_version": "6.0", "capacity": 4}],
                    "instance_num": 1}

    raw = "private-row-value"
    original = SimpleNamespace(sqlstate="22001", diag=SimpleNamespace(
        table_name="resource", column_name="spec",
        message_primary="value too long for type character varying(128)"))
    failure = DataError("INSERT", {"spec": raw}, original)
    monkeypatch.setattr(ledger_sync, "FETCHERS", {"dcs_instance": ledger_sync.FETCHERS["dcs_instance"]})
    monkeypatch.setattr(ledger_sync, "apply_snapshot", Mock(side_effect=failure))
    monkeypatch.setattr(pipeline.platform_watch, "on_run", lambda *args: None)

    run = pipeline.run_task(mem_db, ledger_sync.LEDGER_TASK, client=LedgerClient(), target=TARGET, trigger="MANUAL")

    failed = next(row for row in logged if row.get("event") == "ledger_snapshot_failed")
    started = next(row for row in logged if row.get("event") == "ledger_sync_started")
    finished = next(row for row in logged if row.get("event") == "ledger_sync_finished")
    task_finished = next(row for row in logged if row.get("event") == "task_finished")
    assert run.status == "FAILED"
    assert failed["run_id"] == run.id and failed["resource_type"] == "dcs_instance"
    assert failed["phase"] == "database_apply" and failed["record_count"] == 1 and failed["fetch_calls"] == 1
    assert failed["sqlstate"] == "22001" and failed["column_name"] == "spec"
    assert failed["max_spec_chars"] == len("Redis 6.0 | 4 GB")
    assert failed["db_column_limit"] == 128
    assert started["enabled_count"] == 1 and finished["duration_ms"] >= 0
    assert task_finished["run_id"] == run.id and task_finished["error_count"] == 1
    assert raw not in json.dumps(logged)


def test_disabled_task_does_not_call_cloud_or_emit_info(mem_db, monkeypatch, logged):
    client = Client()
    monkeypatch.setitem(pipeline.TASKS, "logging-disabled", SimpleNamespace(service="ecs", collect=Mock()))
    monkeypatch.setattr(pipeline.collect_switch, "load", lambda db: SimpleNamespace(task_enabled=lambda *a: False))
    run = pipeline.run_task(mem_db, "logging-disabled", client=client, target=TARGET)
    assert run.status == "SKIPPED" and client.calls == 0
    assert not [row for row in logged if row["logger"] == "app.services.pipeline" and row["level"] == "INFO"]


def test_stable_interval_phase_preserves_period_and_cron():
    sync = scheduler.JobSync(Mock(), "UTC")
    anchor = datetime(2026, 10, 27, tzinfo=UTC)
    phases = set()
    for code in ["dcs_status", "ecs_status", "cce_pod_events"]:
        first = sync.trigger(("interval", 60), job_id=code)
        second = sync.trigger(("interval", 60), job_id=code)
        fire = first.get_next_fire_time(None, anchor)
        assert fire == second.get_next_fire_time(None, anchor)
        assert first.interval == timedelta(seconds=60) and first.jitter is None
        assert first.get_next_fire_time(fire, fire + timedelta(milliseconds=1)) - fire == timedelta(seconds=60)
        phases.add(fire)
    assert len(phases) > 1
    assert str(sync.trigger(("cron", "30 3 * * *"), job_id="cleanup")) == str(sync.trigger(("cron", "30 3 * * *")))


def test_same_task_overlap_is_suppressed_without_redis(monkeypatch, logged):
    entered, release = Event(), Event()
    calls = []
    monkeypatch.setattr(scheduler, "_locked", lambda *args: None)

    def job():
        calls.append(1)
        entered.set()
        assert release.wait(2)

    first = Thread(target=lambda: scheduler._with_lock("overlap-test", 60, job))
    first.start()
    try:
        assert entered.wait(2)
        scheduler._with_lock("overlap-test", 60, lambda: calls.append(2))
    finally:
        release.set()
        first.join(2)
    assert calls == [1]
    scheduler._with_lock("overlap-test", 60, lambda: calls.append(3))
    assert calls == [1, 3]
    skips = [row for row in logged if row.get("event") == "job_skipped"]
    assert len(skips) == 1 and skips[0]["level"] == "DEBUG"


def test_redis_publish_failure_is_throttled_closes_connections_and_recovers(monkeypatch, logged):
    error = ConnectionError("unstructured-private-response")
    client = Mock()
    client.publish.side_effect = error
    monkeypatch.setattr(get_settings(), "redis_url", "redis://test.invalid:6379")
    monkeypatch.setattr("redis.Redis.from_url", lambda *a, **kw: client)
    monkeypatch.setattr(events, "_publish_failures", 0)
    monkeypatch.setattr(events, "_publish_warned_at", 0)
    monkeypatch.setattr(events, "monotonic", lambda: 1)
    fallback = Mock()
    monkeypatch.setattr(events.broadcaster, "publish_threadsafe", fallback)
    for _ in range(10):
        events.publish({"alarm": {"id": 1}, "private": "unstructured-private-response"})
    assert fallback.call_count == 10 and client.close.call_count == 10
    assert len([row for row in logged if row.get("event") == "event_publish_fallback"]) == 1
    client.publish.side_effect = None
    events.publish({"alarm": {"id": 1}})
    assert next(row for row in logged if row.get("event") == "event_publish_recovered")["previous_failure_count"] == 10
    assert "unstructured-private-response" not in json.dumps(logged)


def test_slow_subscriber_drops_are_visible_without_event_content_or_log_flood(monkeypatch, logged):
    bus = events.Broadcaster()
    bus.subscribe()
    monkeypatch.setattr(events, "monotonic", lambda: 0)
    for _ in range(205):
        bus._deliver({"private": "unstructured-private-response"})
    rows = [row for row in logged if row.get("event") == "event_subscriber_overflow"]
    assert len(rows) == 1 and rows[0]["subscriber_count"] == 1
    assert "unstructured-private-response" not in json.dumps(logged)


def test_empty_notification_poll_and_minute_are_quiet(mem_db, monkeypatch, logged):
    assert dispatcher.dispatch_pending(mem_db) == 0
    monkeypatch.setattr(notifier, "escalate_unacked", lambda db: 0)
    monkeypatch.setattr(notifier.silence_service, "sweep", lambda db: {"released": 0, "suppressed": 0})
    monkeypatch.setattr(notifier.platform_watch, "check_heartbeats", lambda *a: None)
    notifier.every_minute(mem_db)
    assert not [row for row in logged if row["level"] == "INFO"]


def test_notification_retry_logs_ids_and_safe_trace(mem_db, monkeypatch, logged, delivery_recipient):
    alarm = AlarmEvent(tenant_id=101, fingerprint="notify-logs", rule_id="rule", service="ecs",
                       resource_type="ecs_server", resource_name="server", severity="P2", status="FIRING",
                       title="title", check_item="state")
    mem_db.add(alarm)
    mem_db.flush()
    notice = Notification(alarm_id=alarm.id, kind="FIRING", recipients=[delivery_recipient.email], subject="private subject")
    mem_db.add(notice)
    mem_db.commit()
    monkeypatch.setattr(dispatcher, "render_alarm_mail", lambda *a: ("subject", "private html", "private text"))
    monkeypatch.setattr(dispatcher, "build_message", lambda **kw: EmailMessage())
    monkeypatch.setattr(dispatcher, "_platform_hooks", lambda *a: None)
    error = RuntimeError("unstructured-private-response")

    def fail(message):
        raise error

    assert dispatcher.dispatch_pending(mem_db, sender=fail) == 0
    failure = next(row for row in logged if row.get("event") == "notification_delivery_failed")
    assert failure["notification_id"] == notice.id and failure["alarm_id"] == alarm.id and failure["tenant_id"] == 101
    assert failure["status"] == "RETRYING" and failure["retry"] == 1 and "Traceback" in failure["exc"]
    assert mem_db.scalar(select(Notification)).status == "RETRYING"
    text = json.dumps(logged)
    assert all(value not in text for value in ["unstructured-private-response", "private subject", "private html"])


def test_smtp_failure_logs_safe_stack_without_server_reply(monkeypatch, logged):
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.send_message.side_effect = ConnectionError("unstructured-private-response")
    monkeypatch.setattr(smtp, "_settings", lambda *a: None)
    monkeypatch.setattr(smtp, "_connect_smtp", lambda *a: connection)
    with pytest.raises(smtp.MailTransportError):
        smtp.send(EmailMessage())
    failure = next(row for row in logged if row.get("event") == "smtp_send_failed")
    assert failure["exception_type"] == "ConnectionError" and "Traceback" in failure["exc"]
    assert "unstructured-private-response" not in json.dumps(logged)


def test_bootstrap_selects_worker_service_and_logs_only_counts(mem_db, monkeypatch, logged):
    setup = Mock()
    monkeypatch.setattr(bootstrap, "setup_logging", setup)
    monkeypatch.setattr(bootstrap, "ensure_prod_ready", lambda: None)
    monkeypatch.setattr(bootstrap, "SessionLocal", lambda: nullcontext(mem_db))
    monkeypatch.setattr(bootstrap, "bootstrap", lambda *a, **kw: {"admin_created": False, "rules_added": 2})
    bootstrap.main()
    setup.assert_called_once_with(service="bootstrap")
    result = next(row for row in logged if row.get("event") == "bootstrap_finished")
    assert result["counts"]["rules_added"] == 2 and result["cost_ms"] >= 0 and result["operation_id"]


def test_invalid_rule_logs_safe_warning_once_while_missing_optional_data_is_quiet(monkeypatch, logged):
    monkeypatch.setattr(evaluator, "_error_times", {})
    rule = Rule(id="logging-rule", name="CPU", resource_type="ecs_server", check_item="cpu", kind="threshold",
                expr={"value": "value + 1", "levels": [{"severity": "P2", "threshold": 80}]})
    with log_context(run_id=17, tenant_id=101):
        for _ in range(10):
            assert evaluator.evaluate_rule(rule, {"value": "unstructured-private-response"}, {}, None).action is None
        for _ in range(10):
            assert evaluator.evaluate_rule(rule, {}, {}, None).action is None
    warnings = [row for row in logged if row.get("event") == "rule_evaluation_failed"]
    assert len(warnings) == 1 and warnings[0]["run_id"] == 17 and warnings[0]["tenant_id"] == 101
    assert warnings[0]["level"] == "WARNING" and "Traceback" in warnings[0]["exc"]
    assert "unstructured-private-response" not in json.dumps(logged)
    assert not [row for row in logged if row["level"] == "ERROR"]


def test_dataarts_detail_failures_are_bounded_and_thread_context_reaches_http(logged):
    # 不让异常栈展示的测试源码本身命中敏感内容断言。
    raw = f"private-dataarts-response-{uuid4().hex}"

    def get(service, path, **kwargs):
        if path.endswith("/jobs"):
            return {"jobs": [{"name": f"job-{i}"} for i in range(9)], "total": 9}
        logging.getLogger("app.collectors.dataarts").debug("test_downstream_http")
        raise HcsError(503, "TEST.FAILURE", raw)

    context = RunContext(client=SimpleNamespace(get=get), account="logs", project_id="test-project", tenant_id=101)
    with log_context(run_id=18, request_id="thread-parent"):
        assert dataarts.DataArtsRealtimeCollector().collect(context) == []
    assert len(context.errors) == 9
    traces = [row for row in logged if row.get("event") == "collection_partial_failure"]
    assert len(traces) == 5 and all("Traceback" in row["exc"] and row["run_id"] == 18 for row in traces)
    summary = next(row for row in logged if row.get("event") == "collection_detail_failures")
    assert summary["failed_count"] == 9 and summary["detail_trace_count"] == 5
    calls = [row for row in logged if row["msg"] == "test_downstream_http"]
    assert len(calls) == 9 and all(row["request_id"] == "thread-parent" and row["run_id"] == 18 for row in calls)
    assert raw not in json.dumps(logged)


@pytest.mark.parametrize("collector,phase", [
    (dataarts.DataArtsRealtimeCollector, "job_list"),
    (dataarts.DataArtsBatchCollector, "batch_instances"),
    (mrs.MrsJobCollector, "job_executions"),
])
def test_partial_list_failures_keep_scope_and_trace(collector, phase, logged):
    raw = "unstructured-private-response"

    def get(service, path, **kwargs):
        if path.endswith("/cluster_infos"):
            return {"clusters": [{"clusterId": "cluster-one", "clusterName": "one"}], "clusterTotal": 1}
        raise HcsError(503, "TEST.FAILURE", raw)

    context = RunContext(client=SimpleNamespace(get=get), account="logs", project_id="test-project", tenant_id=101)
    assert collector().collect(context) == []
    failure = next(row for row in logged if row.get("event") == "collection_partial_failure")
    assert failure["phase"] == phase and failure["tenant_id"] == 101 and failure["project_id"] == "test-project"
    assert failure["upstream_status"] == 503 and failure["upstream_code"] == "TEST.FAILURE"
    assert "Traceback" in failure["exc"] and raw not in json.dumps(logged)


def test_mrs_unverified_duration_is_logged_as_counted_data_gap(logged):
    def get(service, path, **_kwargs):
        assert service == "mrs"
        if path.endswith("cluster_infos"):
            return {"clusters": [{"clusterId": "cluster-one", "clusterName": "one"}], "clusterTotal": 1}
        return {"job_list": [{"job_name": "job-one", "submitted_time": 1710000000000,
                              "job_state": "RUNNING", "elapsed_time": 120000}], "total_record": 1}

    context = RunContext(client=SimpleNamespace(get=get), account="logs", project_id="test-project",
                         tenant_id=101, project_ref=201)
    with log_context(run_id=19):
        rows = mrs.MrsJobCollector().collect(context)

    warning = next(row for row in logged if row.get("event") == "collection_data_unavailable")
    assert len(rows) == 1 and rows[0].metrics["elapsed_min"] is None
    assert context.stats["jobs_duration_unit_unverified"] == 1
    assert warning["reason"] == "elapsed_time_unit_unverified" and warning["affected_count"] == 1
    assert warning["run_id"] == 19


def test_optional_prometheus_not_configured_does_not_log_failure(monkeypatch, logged):
    monkeypatch.setattr(ecs, "ecs_source", lambda tenant, policy, **_kwargs: "PROMETHEUS")
    context = RunContext(client=Client(), account="logs", project_id="test-project")
    assert ecs.EcsStatusCollector._metrics(context, ["one"], {}) == ({}, "PROMETHEUS")
    assert context.client.calls == 0
    assert not [row for row in logged if row["level"] in ("WARNING", "ERROR")]


@pytest.mark.parametrize("phase", ["attachment", "sender"])
def test_report_delivery_failure_logs_report_scope_and_stack(mem_db, monkeypatch, logged, phase, delivery_recipient):
    report = reports.generate(mem_db, "LEDGER")
    report.tenant_id = 101
    mem_db.commit()
    monkeypatch.setattr(reports, "build_message", lambda **kw: EmailMessage())
    error = RuntimeError("unstructured-private-response")

    def fail(*args):
        raise error

    if phase == "attachment":
        monkeypatch.setattr(reports, "report_workbook", fail)
    with pytest.raises(RuntimeError if phase == "attachment" else BizError):
        reports.send_report(mem_db, report, sender=fail, recipient_user_ids=[delivery_recipient.id])
    failure = next(row for row in logged if row.get("event") == "report_delivery_failed")
    assert failure["report_id"] == report.id and failure["tenant_id"] == 101 and failure["report_kind"] == "LEDGER"
    assert "Traceback" in failure["exc"] and "unstructured-private-response" not in json.dumps(logged)


def test_manual_worker_logs_failure_and_selects_service(monkeypatch, logged):
    setup = Mock()
    error = RuntimeError("unstructured-private-response")

    def fail():
        raise error

    monkeypatch.setattr(run_once, "setup_logging", setup)
    monkeypatch.setattr(run_once, "_execute", fail)
    with pytest.raises(RuntimeError):
        run_once.main()
    setup.assert_called_once_with(service="run_once")
    failure = next(row for row in logged if row.get("event") == "manual_operation_failed")
    assert failure["operation_id"] and "Traceback" in failure["exc"]
    assert "unstructured-private-response" not in json.dumps(logged)


def test_notification_message_uses_caller_database_without_global_session(mem_db, monkeypatch, delivery_recipient):
    alarm = AlarmEvent(tenant_id=101, fingerprint="local-mail-settings", rule_id="rule", service="ecs",
                       resource_type="ecs_server", resource_name="server", severity="P2", status="FIRING",
                       title="title", check_item="state")
    mem_db.add(alarm)
    mem_db.flush()
    mem_db.add(Notification(alarm_id=alarm.id, kind="FIRING", recipients=[delivery_recipient.email]))
    mem_db.commit()
    monkeypatch.setattr(mail_settings_service, "SessionLocal", Mock(side_effect=AssertionError("global DB forbidden")))
    monkeypatch.setattr(dispatcher, "_platform_hooks", lambda *a: None)
    sent = []
    assert dispatcher.dispatch_pending(mem_db, sender=sent.append) == 1
    assert len(sent) == 1 and isinstance(sent[0], EmailMessage)


def test_ledger_start_can_correlate_with_database_run_created_by_delegate(mem_db, monkeypatch, logged):
    def sync(db, **kwargs):
        run = InspectionRun(task_code=pipeline.LEDGER_TASK, tenant_id=101, status="SUCCESS", stats={})
        db.add(run)
        db.commit()
        return run

    monkeypatch.setattr(pipeline, "sync_ledger", sync)
    run = pipeline.run_task(mem_db, pipeline.LEDGER_TASK, target=TARGET)
    start = next(row for row in logged if row.get("event") == "task_started")
    end = next(row for row in logged if row.get("event") == "task_finished")
    assert start["task_execution_id"] == end["task_execution_id"]
    assert start["task_execution_id"] and end["run_id"] == run.id
    assert start["tenant_id"] == end["tenant_id"] == 101


def test_lock_renewal_thread_keeps_execution_context(monkeypatch, logged):
    renewed = Event()
    lock = Mock()
    error = RuntimeError("unstructured-private-response")

    def fail(*args, **kwargs):
        renewed.set()
        raise error

    lock.extend.side_effect = fail
    monkeypatch.setattr(scheduler, "_locked", lambda *args: lock)
    scheduler._with_lock("renew-context", 0.15, lambda: renewed.wait(2) or pytest.fail("renewal did not run"))
    failure = next(row for row in logged if row.get("event") == "job_lock_renewal_failed")
    assert failure["task"] == "renew-context" and failure["job_id"] and "Traceback" in failure["exc"]
    assert "unstructured-private-response" not in json.dumps(logged)
