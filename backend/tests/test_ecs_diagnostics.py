"""Diagnostic summaries contain counts and fixed reason codes, never source data."""
from app.metrics.ecs_diagnostics import diagnostic_message, log_summary


def test_log_summary_drops_provider_urls_labels_queries_and_unrecognized_reasons():
    private = "https://private.invalid/query?secret=synthetic"
    diagnostic = {
        "status": "no_series", "requested_resources": 201,
        "latest_by_resource": {"private-instance": "private-value"},
        "scope": {"project_id": "private-project"},
        "metrics": {
            "cpu_util": {"reason": "no_unambiguous_ip", "fresh_points": 0, "stale_points": 2,
                         "rejected_points": 1, "labels": {"address": private}},
            "mem_util": {"reason": private, "fresh_points": True, "stale_points": -1},
            "disk_util": {"reason": {"url": private}, "fresh_points": private},
            private: {"reason": "query_failed", "fresh_points": 999},
        },
    }
    summary = log_summary({"metrics_diagnostic": diagnostic, "metrics_source": "PROMETHEUS",
                           "servers": 201, "with_metrics": 2, "sample_points": 3})
    assert summary == {
        "metrics_source": "PROMETHEUS", "metrics_status": "no_series",
        "metrics_reasons": ["no_unambiguous_ip"], "metrics_requested_resources": 201,
        "with_metrics": 2, "sample_points": 3, "metrics_fresh_points": 0,
        "metrics_stale_points": 2, "metrics_rejected_points": 1,
    }
    assert "私网 IP" in diagnostic_message(diagnostic)
    assert "private" not in str(summary) + diagnostic_message(diagnostic)


def test_unknown_or_malformed_diagnostics_use_safe_fallbacks():
    for value in (None, [], "secret"):
        assert log_summary({"metrics_diagnostic": value}) == {}
    diagnostic = {"status": "private-error", "metrics": "private-data"}
    summary = log_summary({"metrics_diagnostic": diagnostic, "metrics_source": "private-source"})
    assert summary["metrics_status"] == "unknown" and summary["metrics_source"] == "unknown"
    assert "private" not in str(summary) + diagnostic_message(diagnostic)
