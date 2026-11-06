"""指标缺失或损坏不能变为健康的零，也不能让状态采集因数值转换中断。"""
from types import SimpleNamespace

import pytest

from app.hcs.errors import HcsError
from app.k8s.kube import aggregate, parse_metric
from app.metrics.ces import ces_list_metrics, ces_query
from app.metrics.prometheus import ecs_metrics


@pytest.mark.parametrize("value", [None, "bad", True, float("nan"), float("inf"), -float("inf")])
def test_ces_bad_latest_value_is_missing_not_zero(value):
    client = SimpleNamespace(post=lambda *_a, **_k: {"metrics": [{
        "dimensions": [{"name": "id", "value": "resource"}], "metric_name": "memory",
        "datapoints": [{"timestamp": 1, "average": value}],
    }]})
    assert ces_query(client, [{}]) == []


@pytest.mark.parametrize("value", ["+Inf", "-Inf", "NaN", "bad"])
def test_prometheus_nonfinite_samples_do_not_crash_boot_time_or_create_metrics(value):
    client = SimpleNamespace(query=lambda _: [{"metric": {"instance": "10.0.0.1:9100"}, "value": [1, value]}])
    assert ecs_metrics(client, {"10.0.0.1": "resource"}) == {}


def test_prometheus_short_sample_vector_is_ignored():
    client = SimpleNamespace(query=lambda _: [{"metric": {"instance": "10.0.0.1:9100"}, "value": [1]}])
    assert ecs_metrics(client, {"10.0.0.1": "resource"}) == {}


def test_valid_zero_is_still_a_real_metric():
    client = SimpleNamespace(query=lambda _: [{"metric": {"instance": "10.0.0.1:9100"}, "value": [1, "0"]}])
    assert ecs_metrics(client, {"10.0.0.1": "resource"})["resource"]["cpu_util"] == 0


@pytest.mark.parametrize("value", ["NaN", "+Inf", "-Inf"])
def test_probe_invalid_samples_do_not_become_recovery_values(value):
    values = parse_metric("backpressure " + value, "backpressure")
    assert values == [] and aggregate(values, "max") is None


def test_aggregate_of_unavailable_values_is_not_a_valid_probe_reading():
    assert aggregate([float("nan"), float("inf")], "max") is None


def test_ces_follows_explicit_marker_even_when_page_is_short():
    tokens = []
    def get(*_a, **kwargs):
        marker = kwargs["params"].get("start")
        tokens.append(marker)
        return {"metrics": [{"name": "first"}], "meta_data": {"marker": "next"}} if marker is None else {
            "metrics": [{"name": "last"}], "meta_data": {}}
    assert ces_list_metrics(SimpleNamespace(get=get), "namespace", "metric") == [{"name": "first"}, {"name": "last"}]
    assert tokens == [None, "next"]


def test_ces_marker_budget_cannot_be_a_successful_partial_discovery():
    client = SimpleNamespace(get=lambda *_a, **_k: {"metrics": [{"name": "first"}], "meta_data": {"marker": "next"}})
    with pytest.raises(HcsError):
        ces_list_metrics(client, "namespace", "metric", limit=1, max_pages=1)
