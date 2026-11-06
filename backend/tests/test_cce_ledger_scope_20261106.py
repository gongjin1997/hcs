"""CCE 管理面在任何节点/原生指标请求之前，按本租户项目的台账字段判定范围。"""
from datetime import timedelta

import pytest
from sqlalchemy import event, select

from app.models import CceClusterSnapshot, Resource
from app.services import cce_monitoring as service
from app.services.collect_scope import Scopes

from .test_cce_management_20261106 import NOW, Client, cluster, context


def resource(db, cid="cluster-a", *, tenant=1, project=10, **fields):
    row = Resource(service="cce", resource_type="cce_cluster", resource_id=cid, name=cid,
                   tenant_id=tenant, project_ref=project, **fields)
    db.add(row)
    db.commit()
    return row


def collect(db, rules, client=None):
    ctx = context(db, client)
    ctx.scopes = Scopes(raw={(1, "cce_cluster"): {"rules": rules}})
    service.CceHealthCollector().collect(ctx)
    return ctx


@pytest.mark.parametrize("dimension,excluded,included", [
    ("env", {"env_type": "TEST"}, {"env_type": "PROD"}),
    ("business", {"business": "sandbox"}, {"business": "billing"}),
    ("owner", {"owner": "team-test"}, {"owner": "team-prod"}),
    ("critical", {"critical": False}, {"critical": True}),
])
def test_ledger_dimensions_exclude_before_detail_queries_and_preserve_snapshot(
        mem_db, monkeypatch, dimension, excluded, included):
    row = resource(mem_db, **included)
    collect(mem_db, [])
    cached = mem_db.scalar(select(CceClusterSnapshot))
    cached.last_seen = NOW - timedelta(days=1)
    before = cached.last_seen, cached.cluster, cached.sources, cached.nodes, cached.native
    for field, value in excluded.items():
        setattr(row, field, value)
    mem_db.commit()
    native_calls = []

    def native(*args):
        native_calls.append(args)
        return {}

    monkeypatch.setattr(service.CceHealthCollector, "_native", staticmethod(native))
    value = next(iter(included.values()))
    value = str(value).lower() if isinstance(value, bool) else value
    client = Client()
    ctx = collect(mem_db, [{"effect": "include", "match": {dimension: [value]}}], client)
    assert len(client.requests) == 1 and not native_calls
    assert ctx.stats["cce_health"]["clusters"] == 0 and not ctx.errors
    assert not cached.missing
    assert (cached.last_seen, cached.cluster, cached.sources, cached.nodes, cached.native) == before


@pytest.mark.parametrize("tenant,project", [(1, 20), (2, 20)])
def test_foreign_ledger_fields_do_not_include_the_current_cluster(mem_db, tenant, project):
    resource(mem_db, tenant=tenant, project=project, env_type="PROD")
    client = Client()
    collect(mem_db, [{"effect": "include", "match": {"env": ["PROD"]}}], client)
    assert len(client.requests) == 1
    assert mem_db.scalar(select(CceClusterSnapshot)) is None


def test_missing_ledger_row_uses_known_empty_fields_for_exclusion_rules(mem_db):
    client = Client()
    ctx = collect(mem_db, [{"effect": "exclude", "match": {"env": ["PROD"]}}], client)
    assert len(client.requests) == 3 and ctx.stats["cce_health"]["clusters"] == 1


@pytest.mark.parametrize("rules,expected_reads", [
    ([], 0),
    ([{"effect": "include", "match": {"cluster": ["*"]}}], 0),
    ([{"effect": "include", "match": {"env": ["PROD"]}}], 1),
])
def test_ledger_fields_are_read_in_one_scoped_batch_only_when_needed(mem_db, rules, expected_reads):
    ids = [f"cluster-{i}" for i in range(12)]
    for cid in ids:
        resource(mem_db, cid, env_type="PROD")
    queries = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().lower().startswith("select") and "from resource" in statement.lower():
            queries.append(statement)

    bind = mem_db.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        client = Client(listed=[cluster(cid) for cid in ids])
        ctx = collect(mem_db, rules, client)
    finally:
        event.remove(bind, "before_cursor_execute", record)
    assert len(queries) == expected_reads
    assert ctx.stats["cce_health"]["clusters"] == len(ids)
    assert len(client.requests) == 1 + 2 * len(ids)
