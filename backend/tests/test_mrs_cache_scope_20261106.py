"""终态缓存必须覆盖完整实例，不能把范围过滤后的资源当作完整节点清单。"""
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.collectors.mrs import MrsJobCollector
from app.models import Resource
from app.services.collect_scope import Scopes

from .test_mrs_hcs851_contract import NOW, DataArtsClient, inst, job_ctx, node


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr("app.collectors.mrs.time.time", lambda: NOW / 1000)


def collect(db, *, rules=None, tenant=11, project=111):
    client = DataArtsClient([inst("etl", 7)], {("etl", 7): [node("a"), node("b", status="fail")]})
    ctx = job_ctx(client)
    ctx.db, ctx.tenant_id, ctx.project_ref = db, tenant, project
    ctx.scopes = Scopes(raw={(tenant, "mrs_job"): {"rules": rules or []}})
    return MrsJobCollector().collect(ctx), client, ctx


def persist(db, observations, *, tenant=11, project=111):
    for observation in observations:
        row = db.scalar(select(Resource).where(
            Resource.tenant_id == tenant, Resource.project_ref == project,
            Resource.resource_type == "mrs_job", Resource.resource_id == observation.resource_id))
        if row is None:
            row = Resource(service="mrs", resource_type="mrs_job", resource_id=observation.resource_id,
                           name=observation.name, tenant_id=tenant, project_ref=project)
            db.add(row)
        row.attrs, row.metrics = deepcopy(observation.attrs), deepcopy(observation.metrics)
    db.commit()


def test_expanding_scope_fetches_the_missing_node_then_reuses_the_complete_instance(mem_db):
    first, _, _ = collect(mem_db, rules=[{"effect": "exclude", "match": {"node": ["b"]}}])
    assert [o.attrs["node_name"] for o in first] == ["a"]
    persist(mem_db, first)

    restored, client, ctx = collect(mem_db)
    assert {o.attrs["node_name"] for o in restored} == {"a", "b"}
    assert next(o for o in restored if o.attrs["node_name"] == "b").facts["node_status"] == "fail"
    assert len(client.details()) == 1 and not ctx.errors
    persist(mem_db, restored)

    cached, client, ctx = collect(mem_db)
    assert {o.attrs["node_name"] for o in cached} == {"a", "b"}
    assert client.details() == [] and ctx.stats["mrs_jobs"]["details_reused"] == 1


def test_legacy_resource_without_completeness_evidence_is_refetched(mem_db):
    mem_db.add(Resource(service="mrs", resource_type="mrs_job", resource_id="ws-1/etl/a", name="etl / a",
                        tenant_id=11, project_ref=111, attrs={
                            "workspace": "ws-1", "job_name": "etl", "node_name": "a", "node_type": "MRSSpark",
                            "source": "dataarts_instance_detail", "instance_id": 7,
                            "job_status": "success", "node_status": "success"}))
    mem_db.commit()
    observations, client, _ = collect(mem_db)
    assert {o.attrs["node_name"] for o in observations} == {"a", "b"}
    assert len(client.details()) == 1


@pytest.mark.parametrize("missing", ["deleted", "absent"])
def test_incomplete_stored_instance_cannot_reuse_a_surviving_sibling(mem_db, missing):
    first, _, _ = collect(mem_db)
    persist(mem_db, first)
    sibling = mem_db.scalar(select(Resource).where(Resource.resource_id == "ws-1/etl/b"))
    if missing == "deleted":
        sibling.lifecycle = "DELETED"
    else:
        mem_db.delete(sibling)
    mem_db.commit()
    observations, client, _ = collect(mem_db)
    assert {o.attrs["node_name"] for o in observations} == {"a", "b"}
    assert len(client.details()) == 1


@pytest.mark.parametrize("tenant,project", [(11, 112), (22, 222)])
def test_other_tenant_or_project_cannot_complete_a_partial_cache(mem_db, tenant, project):
    first, _, _ = collect(mem_db, rules=[{"effect": "exclude", "match": {"node": ["b"]}}])
    persist(mem_db, first)
    foreign, _, _ = collect(mem_db, tenant=tenant, project=project)
    persist(mem_db, foreign, tenant=tenant, project=project)
    observations, client, _ = collect(mem_db)
    assert {o.attrs["node_name"] for o in observations} == {"a", "b"}
    assert len(client.details()) == 1


def test_a_complete_cache_is_still_filtered_by_the_current_node_scope(mem_db):
    first, _, _ = collect(mem_db)
    persist(mem_db, first)
    observations, client, _ = collect(mem_db, rules=[{"effect": "exclude", "match": {"node": ["b"]}}])
    assert [o.attrs["node_name"] for o in observations] == ["a"] and client.details() == []
