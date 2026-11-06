"""0007 的真实升降级：保留原记录、保守回填项目，并拒绝有损降级。"""
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import MetaData, Table, create_engine, inspect, select
from sqlalchemy.orm import Session

from app.collectors.base import RunContext
from app.core.db import Base
from app.k8s import collector
from app.models import HcsProject, PodEvent, PodState, Resource
from app.models.pod import SCOPE_META, pod_event_key, pod_state_key

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture()
def legacy_pods(migration_url):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.attributes["database_url"] = migration_url
    command.upgrade(cfg, "0006")
    engine = create_engine(migration_url)
    metadata = MetaData()
    tables = {name: Table(name, metadata, autoload_with=engine) for name in ("pod_state", "pod_event", "hcs_account")}
    with Session(engine) as db:
        for tid in (101, 202):
            now = NOW
            db.execute(tables["hcs_account"].insert().values(
                id=tid, code=f"test-{tid}", name=f"测试租户 {tid}", short_name="", color="#1d63f0",
                iam_endpoint="https://iam.invalid", domain_name="test", auth_type="PASSWORD", secret_cipher="",
                endpoints={}, enabled=True, report_mail_to=[], metrics_source="NONE", capabilities={},
                last_check={}, created_at=now, updated_at=now))
        db.flush()
        for tid, pid in ((101, 1001), (101, 1002), (202, 2002)):
            db.add(HcsProject(id=pid, account_id=tid, region_id="test", project_id=f"p-{pid}",
                              external_domain="test.invalid"))
        db.commit()
    try:
        yield cfg, engine, tables
    finally:
        engine.dispose()


def old_state(connection, table, *, cid="cluster", tenant=101, name="pod", key=None, data=None):
    key = key or f"{cid}/ns/{name}"
    connection.execute(table.insert().values(
        key=key, tenant_id=tenant, cluster_id=cid, cluster_name=cid, namespace="ns", pod=name,
        workload_kind="Deployment", workload="worker", phase="Running", ready=True, restarts=3,
        labels={}, containers=[], data={} if data is None else data, deleted=False, first_seen=NOW, last_seen=NOW))
    return key


def old_event(connection, table, *, cid="cluster", tenant=101, name="pod", dedup="k8s:uid", detail=None):
    result = connection.execute(table.insert().values(
        tenant_id=tenant, cluster_id=cid, cluster_name=cid, namespace="ns", pod=name,
        workload_kind="Deployment", workload="worker", container="main", event_type="BACKPRESSURE",
        level="WARNING", source="K8S_EVENT", reason="Backpressure", message="保留的原事件", count=3,
        first_time=NOW, last_time=NOW, ongoing=True, detail={} if detail is None else detail,
        dedup_key=dedup, created_at=NOW))
    return result.inserted_primary_key[0]


def resource(engine, cid, tenant, project, *, resource_type="cce_cluster", lifecycle="ACTIVE"):
    with Session(engine) as db:
        db.execute(Resource.__table__.insert().values(
            service="cce", resource_type=resource_type, resource_id=cid, name=cid,
            tenant_id=tenant, project_ref=project, lifecycle=lifecycle))
        db.commit()


def rows(engine, name):
    table = Table(name, MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(select(table)).mappings()]


def test_upgrade_only_backfills_unambiguous_owned_cce_resource_and_preserves_records(legacy_pods):
    cfg, engine, tables = legacy_pods
    resource(engine, "known", 101, 1001, lifecycle="DELETED")
    resource(engine, "ambiguous", 101, 1001)
    resource(engine, "ambiguous", 101, 1002)
    resource(engine, "unknown-and-known", 101, 1001)
    resource(engine, "unknown-and-known", 101, None)
    resource(engine, "wrong-owner", 101, 2002)
    resource(engine, "other-tenant", 202, 2002)
    resource(engine, "only-workload", 101, 1001, resource_type="cce_workload")
    resource(engine, "missing-project", 101, 9999)
    cids = ("known", "ambiguous", "unknown-and-known", "wrong-owner", "other-tenant", "only-workload",
            "missing-project", "no-resource")
    with engine.begin() as connection:
        for cid in cids:
            old_state(connection, tables["pod_state"], cid=cid, data={"bp1": {"over_since": NOW.isoformat()}})
            old_event(connection, tables["pod_event"], cid=cid, dedup=f"k8s:{cid}", detail={"original": cid})
    before_states, before_events = rows(engine, "pod_state"), rows(engine, "pod_event")
    command.upgrade(cfg, "0007")
    with Session(engine) as db:
        states = db.scalars(select(PodState)).all()
        events = db.scalars(select(PodEvent)).all()
        assert len(states) == len(events) == len(cids)
        for st in states:
            expected = 1001 if st.cluster_id == "known" else None
            assert st.project_ref == expected
            assert st.key == pod_state_key(101, expected, st.cluster_id, "ns", "pod")
            assert st.data["bp1"] == {"over_since": NOW.isoformat()}
        for event in events:
            expected = 1001 if event.cluster_id == "known" else None
            assert event.project_ref == expected
            assert event.dedup_key == pod_event_key(101, expected, event.cluster_id, f"k8s:{event.cluster_id}")
            assert event.message == "保留的原事件" and event.count == 3 and event.ongoing
            assert event.first_time == NOW and event.last_time == NOW
        assert {event.id for event in events} == {event["id"] for event in before_events}
    command.upgrade(cfg, "head")
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    command.downgrade(cfg, "0006")
    assert sorted(rows(engine, "pod_state"), key=lambda r: r["key"]) == sorted(before_states, key=lambda r: r["key"])
    assert rows(engine, "pod_event") == before_events
    command.upgrade(cfg, "0007")
    assert len(rows(engine, "pod_state")) == len(cids)


@pytest.mark.parametrize("previous", [None, {"custom": [1, 2]}, "legacy-value"])
def test_reserved_meta_preserves_live_probe_fields_and_restores_previous_value(legacy_pods, previous):
    cfg, engine, tables = legacy_pods
    original = {SCOPE_META: previous, "bp7": {"over_since": NOW.isoformat(), "peak": 42}, "other": [1, "a"]}
    detail = {SCOPE_META: previous, "probe_id": 7, "value": 42}
    with engine.begin() as connection:
        original_key = old_state(connection, tables["pod_state"], data=original)
        event_id = old_event(connection, tables["pod_event"], detail=detail)
    command.upgrade(cfg, "0007")
    with Session(engine) as db:
        st = db.scalar(select(PodState))
        assert st.data["bp7"] == original["bp7"] and st.data["other"] == original["other"]
        assert st.data[SCOPE_META] == {"key": original_key, "previous": previous}
        assert db.get(PodEvent, event_id).detail["probe_id"] == 7
    command.downgrade(cfg, "0006")
    assert rows(engine, "pod_state")[0]["data"] == original
    assert rows(engine, "pod_event")[0]["detail"] == detail


def test_non_dict_legacy_json_roundtrips_without_losing_data(legacy_pods):
    cfg, engine, tables = legacy_pods
    with engine.begin() as connection:
        old_state(connection, tables["pod_state"], data=["old", {"value": 3}])
        old_event(connection, tables["pod_event"], detail=["keep"])
    command.upgrade(cfg, "0007")
    command.downgrade(cfg, "0006")
    assert rows(engine, "pod_state")[0]["data"] == ["old", {"value": 3}]
    assert rows(engine, "pod_event")[0]["detail"] == ["keep"]


@pytest.mark.parametrize("event_only", [False, True])
def test_downgrade_refuses_cross_scope_cluster_without_mutating_any_row(legacy_pods, event_only):
    cfg, engine, _tables = legacy_pods
    command.upgrade(cfg, "0007")
    with Session(engine) as db:
        for tenant, project in ((101, 1001), (202, 2002)):
            if not event_only:
                db.add(PodState(key=pod_state_key(tenant, project, "same", "ns", "pod"), tenant_id=tenant,
                                project_ref=project, cluster_id="same", cluster_name="same", namespace="ns", pod="pod"))
            db.add(PodEvent(tenant_id=tenant, project_ref=project, cluster_id="same", cluster_name="same",
                            namespace="ns", pod="pod", event_type="RESTART", level="WARNING", source="POD_STATUS",
                            first_time=NOW, last_time=NOW, dedup_key=pod_event_key(tenant, project, "same", "raw"),
                            detail={SCOPE_META: {"dedup": "raw"}}))
        db.commit()
    before = (rows(engine, "pod_state"), rows(engine, "pod_event"))
    with pytest.raises(RuntimeError, match="无法安全降级"):
        command.downgrade(cfg, "0006")
    assert (rows(engine, "pod_state"), rows(engine, "pod_event")) == before
    assert "project_ref" in {c["name"] for c in inspect(engine).get_columns("pod_state")}
    assert rows(engine, "alembic_version")[0]["version_num"] == "0007"


def test_downgrade_refuses_dedup_collision_even_when_cluster_ids_differ(legacy_pods):
    cfg, engine, _tables = legacy_pods
    command.upgrade(cfg, "0007")
    with Session(engine) as db:
        for cid in ("one", "two"):
            db.add(PodEvent(tenant_id=101, project_ref=1001, cluster_id=cid, cluster_name=cid,
                            namespace="ns", event_type="RESTART", level="WARNING", source="POD_STATUS",
                            first_time=NOW, last_time=NOW, dedup_key=pod_event_key(101, 1001, cid, "same-raw"),
                            detail={SCOPE_META: {"dedup": "same-raw"}}))
        db.commit()
    before = rows(engine, "pod_event")
    with pytest.raises(RuntimeError, match="无法安全降级"):
        command.downgrade(cfg, "0006")
    assert rows(engine, "pod_event") == before


@pytest.mark.parametrize("detail", [{SCOPE_META: {"dedup": "x" * 161}}, {}, {SCOPE_META: {"unrelated": "value"}}])
def test_downgrade_refuses_overlong_or_lost_event_original_key(legacy_pods, detail):
    cfg, engine, _tables = legacy_pods
    command.upgrade(cfg, "0007")
    with Session(engine) as db:
        db.add(PodEvent(tenant_id=101, project_ref=1001, cluster_id="cluster", cluster_name="cluster", namespace="ns",
                        event_type="RESTART", level="WARNING", source="POD_STATUS", first_time=NOW, last_time=NOW,
                        dedup_key=pod_event_key(101, 1001, "cluster", "original"), detail=detail))
        db.commit()
    before = rows(engine, "pod_event")
    with pytest.raises(RuntimeError, match="无法安全降级"):
        command.downgrade(cfg, "0006")
    assert rows(engine, "pod_event") == before


def test_duplicate_old_state_identity_blocks_upgrade_before_schema_or_data_changes(legacy_pods):
    cfg, engine, tables = legacy_pods
    with engine.begin() as connection:
        old_state(connection, tables["pod_state"], key="original-a")
        old_state(connection, tables["pod_state"], key="original-b")
    before = rows(engine, "pod_state")
    with pytest.raises(RuntimeError, match="同作用域重复"):
        command.upgrade(cfg, "0007")
    assert rows(engine, "pod_state") == before
    assert "project_ref" not in {c["name"] for c in inspect(engine).get_columns("pod_state")}
    assert rows(engine, "alembic_version")[0]["version_num"] == "0006"


def test_migrated_ongoing_probe_and_timers_continue_without_duplicate_events(legacy_pods, monkeypatch):
    cfg, engine, tables = legacy_pods
    resource(engine, "cluster", 101, 1001)
    raw = "bp:7:cluster/ns/pod"
    data = {SCOPE_META: {"legacy": True}, "bp7": {"over_since": NOW.isoformat(), "last": 42}}
    detail = {SCOPE_META: "legacy", "probe_id": 7}
    with engine.begin() as connection:
        old_state(connection, tables["pod_state"], data=data)
        event_id = old_event(connection, tables["pod_event"], dedup=raw, detail=detail)
    command.upgrade(cfg, "0007")
    monkeypatch.setattr(collector, "utcnow", lambda: NOW)
    with Session(engine) as db:
        context = RunContext(client=None, account="test", project_id="test", tenant_id=101, project_ref=1001, db=db)
        run = collector._Run(context, NOW)
        event = db.get(PodEvent, event_id)
        st = db.scalar(select(PodState))
        run.ongoing[event.dedup_key] = event
        continued = run._ongoing(st, ("cluster", "cluster"), raw, "BACKPRESSURE", first=NOW,
                                 reason="BackPressure", message="continued", count=4, source="PROBE", detail={"last": 43})
        assert continued.id == event_id and st.data["bp7"] == data["bp7"]
        assert continued.detail["probe_id"] == 7
        db.commit()
    assert len(rows(engine, "pod_event")) == 1
    command.downgrade(cfg, "0006")
    restored = rows(engine, "pod_state")[0]["data"]
    assert restored == data
    restored_detail = rows(engine, "pod_event")[0]["detail"]
    assert restored_detail == {**detail, "last": 43}
