"""AOM compatibility DTOs remain readable; every legacy query is disabled locally."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.collectors.base import RunContext
from app.hcs import HcsError
from app.hcs.ratelimit import consume_budget, request_budget
from app.metrics import aom

START = datetime(2026, 10, 1, tzinfo=UTC)
MAPPING = {"namespace": "CUSTOM.Test", "dimension": "instance_id", "metrics": {"cpu_util": "cpu"}}
DESCRIPTOR = {"namespace": "CUSTOM.Test", "metric_name": "cpu", "dimensions": [
    {"name": "instance_id", "value": "one"}]}


class ForbiddenClient:
    def __getattr__(self, name):
        pytest.fail(f"Disabled AOM must not inspect client or read cache: {name}")


@pytest.mark.parametrize("operation", [
    lambda client: aom.aom_probe(client),
    lambda client: aom.aom_list_metrics(client, "CUSTOM.Test"),
    lambda client: aom.aom_list_metrics(client, "CUSTOM.Test", "cpu", DESCRIPTOR["dimensions"], limit=1,
                                      all_pages=True, prometheus_instance_id="old-prom"),
    lambda client: aom.discover(client, [DESCRIPTOR]),
    lambda client: aom.discover(client, []),
    lambda client: aom.aom_query(client, [DESCRIPTOR], START, START+timedelta(minutes=5)),
    lambda client: aom.aom_query(client, [], START, START),
    lambda client: aom.resolve_series(client, ["one"], aom.AomMapping.model_validate(MAPPING)),
    lambda client: aom.resolve_series(client, [], aom.AomMapping.model_validate(MAPPING)),
], ids=["probe", "metadata", "metadata-options", "discovery", "empty-discovery", "samples", "empty-samples",
        "resolve", "empty-resolve"])
def test_every_legacy_query_raises_disabled_before_client_or_budget(operation):
    with request_budget(attempts=1, seconds=5):
        with pytest.raises(HcsError) as error:
            operation(ForbiddenClient())
        assert error.value.code == "LOCAL.AOM_DISABLED" and error.value.status == 0
        # The blocked read must not consume the enclosing cloud-request budget.
        consume_budget()


@pytest.mark.parametrize("ids", [[], ["one"], ["one", "two"]])
@pytest.mark.parametrize("raw", [None, {}, MAPPING, "legacy-malformed"])
def test_latest_is_empty_and_explicitly_disabled_without_client_or_cache(ids, raw):
    tenant = SimpleNamespace(metrics_source="AOM", endpoints={"aom": "https://aom.invalid"},
                             capabilities={"aom": {"available": True}})
    ctx = RunContext(client=ForbiddenClient(), account="synthetic", project_id="p1", tenant=tenant,
                     params={"aom": raw})
    ctx.stats.update(aom_disabled=False, aom_mapping_configured=True, unrelated="kept")
    assert aom.aom_latest(ctx, ids) == {}
    assert ctx.stats["aom_disabled"] is True and ctx.stats["aom_mapping_configured"] is False
    assert ctx.stats["unrelated"] == "kept" and ctx.errors == []
    assert tenant.capabilities == {"aom": {"available": True}}
    assert not hasattr(aom, "_cache") and not hasattr(aom, "metric_key")


def test_legacy_data_structures_remain_parseable_without_enabling_aom():
    mapping = aom.AomMapping.model_validate(MAPPING)
    assert mapping.namespace == "CUSTOM.Test" and mapping.metrics == {"cpu_util": "cpu"}
    point = aom.AomPoint(at=START, unit="Percent", statistics={"average": 0})
    series = aom.AomSeries(metric=DESCRIPTOR, points=[point], partial=True)
    assert series.points[0].statistics["average"] == 0 and series.partial is True
    assert series.points[0].at == START


@pytest.mark.parametrize("extra", [{"namespace": " "}, {"prometheus_instance_id": "id\r\nheader"},
                                   {"period": 600}, {"metrics": {"cpu_usage": " "}}, {"headers": {}}])
def test_mapping_rejects_invalid_or_unrestricted_configuration(extra):
    body = {"namespace": "CUSTOM.Test", "dimension": "instance_id", "metrics": {"cpu_usage": "cpu"}, **extra}
    with pytest.raises(ValidationError):
        aom.AomMapping.model_validate(body)
