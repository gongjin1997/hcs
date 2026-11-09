"""0022 在独立临时数据库升级/降级：保留主键、告警、证据、权限边界。"""
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.alarms.fingerprint import fingerprint
from app.models import (
    AlarmEvent,
    AlarmEventLog,
    AlarmRule,
    CollectSwitch,
    EvalState,
    HcsAccount,
    HcsProject,
    InspectionTask,
    Resource,
    ResourceChange,
    SysSetting,
)
from app.services.bootstrap import ensure_tasks
from app.services.collect_scope import load as scopes
from app.services.collect_switch import load as switches
from app.services.inspection_evidence import ScopeContext, scope_key

NOW = datetime(2026, 11, 9, tzinfo=UTC)
NODE_EXPR = {"when": "node_status == 'fail'", "severity": "P2", "critical_severity": "P1",
             "value_field": "node_status", "title": "MRS 作业节点执行失败（{node_status}）"}
RUNBOOK = ("1. 在 DataArts Studio 实例监控中打开该作业实例，查看失败 MRS 节点的日志（logPath）；\n"
           "2. 跳转 MRS Manager / YARN 查看对应 application 的错误与资源队列，资源不足时调整队列配额；\n"
           "3. 修复后在 DataArts 重跑失败实例，下一次实例中该节点成功即自动恢复。\n")


def config(url):
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "migrations"))
    cfg.attributes["database_url"] = url
    return cfg


def seed(engine, *, scoped=True, enabled=True, customized=False, broken_fp=False):
    with Session(engine) as db:
        db.add(HcsAccount(id=11, code="synthetic", name="Synthetic", iam_endpoint="http://invalid",
                          domain_name="synthetic"))
        db.flush()
        db.add(HcsProject(id=111, account_id=11, region_id="test", project_id="project-one", external_domain="invalid"))
        db.add(InspectionTask(code="mrs_job_status", name="MRS", service="mrs", interval_sec=713,
                              cron="*/11 * * * *", enabled=enabled, params={"window_hours": 24, "max_details": 13}))
        db.add_all([CollectSwitch(tenant_id=0, resource_type="mrs_job", enabled=False),
                    CollectSwitch(tenant_id=11, resource_type="mrs_job", enabled=True)])
        if scoped:
            db.add(SysSetting(key="collect_scope:0:mrs_job", value={"rules": [
                {"effect": "include", "match": {"workspace": ["ws-authorized"], "job": ["etl-*"]}}]},
                updated_by="operator"))
        node = Resource(tenant_id=11, project_ref=111, service="mrs", resource_type="mrs_job",
                        resource_id="ws-authorized/etl-a/sql", name="etl-a / sql", status_raw="fail",
                        attrs={"source": "dataarts_instance_detail", "workspace": "ws-authorized", "job_name": "etl-a",
                               "node_name": "sql", "node_type": "MRSSpark", "instance_id": 7,
                               "inspection_monitoring": {"task_code": "mrs_job_status", "checked_rules": 1}},
                        metrics={"run_minutes": 1})
        native = Resource(tenant_id=11, project_ref=111, service="mrs", resource_type="mrs_job", resource_id="c/native",
                          name="native", attrs={"source": "mrs_job_execution", "job_id": "native"})
        db.add_all([node, native])
        db.flush()
        fp = fingerprint("synthetic", "project-one", "mrs_job", node.resource_id, "R-MRS-JOB-001")
        alarm = AlarmEvent(tenant_id=11, resource_ref=node.id, resource_type="mrs_job", service="mrs", resource_name=node.name,
                           fingerprint="unverifiable" if broken_fp else fp, rule_id="R-MRS-JOB-001", status="ACKED",
                           severity="P2", title="failure", check_item="job_status", ack_by="operator", count=9)
        db.add(alarm)
        db.flush()
        db.add(AlarmEventLog(alarm_id=alarm.id, action="ACK", operator="operator", note="retain me"))
        db.add(ResourceChange(tenant_id=11, resource_ref=node.id, resource_type="mrs_job", resource_id=node.resource_id,
                              name=node.name, change_type="STATUS", changes=[]))
        db.add(EvalState(fingerprint=fp, data={"hits": 5, "ok_hits": 0, "_inspection_evidence": {
            "resource_ref": node.id, "resource_type": "mrs_job", "resource_id": node.resource_id,
            "rule_id": "R-MRS-JOB-001", "task_code": "mrs_job_status", "check_item": "job_status",
            "execution_id": "7", "since": NOW.isoformat()}, "_evidence_scope": "previous"}))
        db.add(AlarmRule(id="R-MRS-JOB-001", name="MRS 作业执行失败", resource_type="mrs_job", check_item="job_status",
                         kind="state", expr=deepcopy(NODE_EXPR), recover={"when": "node_status == 'success'"},
                         runbook=RUNBOOK + ("operator note" if customized else ""),
                         updated_by="system:dataarts-mrs-v1", version=2))
        db.add(AlarmRule(id="R-OPERATOR", name="custom", resource_type="mrs_job", check_item="job_status", kind="state",
                         expr={"when": "node_status == 'fail'", "severity": "P1"}, recover={}, updated_by="operator"))
        db.commit()
        return node.id, native.id, alarm.id, fp


def test_split_retains_nodes_alarms_history_fingerprints_scope_and_schedule(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0021")
    engine = create_engine(migration_url)
    try:
        node_id, native_id, alarm_id, old_fp = seed(engine)
        command.upgrade(cfg, "0022")
        new_fp = fingerprint("synthetic", "project-one", "dgc_mrs_node", "ws-authorized/etl-a/sql", "R-MRS-JOB-001")
        with Session(engine) as db:
            node, native = db.get(Resource, node_id), db.get(Resource, native_id)
            assert node.resource_type == "dgc_mrs_node" and node.service == "dataarts"
            assert native.resource_type == "mrs_job" and native.service == "mrs"
            event = db.get(AlarmEvent, alarm_id)
            assert (event.status, event.ack_by, event.count, event.fingerprint) == ("ACKED", "operator", 9, new_fp)
            assert event.resource_type == "dgc_mrs_node" and event.service == "dataarts"
            assert db.scalar(select(AlarmEventLog)).note == "retain me"
            assert len(db.scalars(select(AlarmEventLog)).all()) == 1
            assert db.scalar(select(ResourceChange)).resource_type == "dgc_mrs_node"
            state = db.get(EvalState, new_fp)
            assert state.data["hits"] == 5 and db.get(EvalState, old_fp) is None
            assert state.data["_inspection_evidence"]["task_code"] == "dgc_mrs_node_status"
            assert state.data["_evidence_scope"] == scope_key(ScopeContext(
                "synthetic", "project-one", 11, 111, {"_task_code": "dgc_mrs_node_status"}))
            old, new = db.get(InspectionTask, "mrs_job_status"), db.get(InspectionTask, "dgc_mrs_node_status")
            assert old.enabled and new.enabled and old.interval_sec == new.interval_sec == 713
            assert new.params == old.params and new.cron == old.cron
            sw = switches(db)
            assert sw.enabled(11, "dgc_mrs_node") and not sw.enabled(None, "dgc_mrs_node")
            assert not sw.enabled(11, "mrs_job") and not sw.enabled(22, "mrs_job")
            assert scopes(db).rules(11, "dgc_mrs_node") == scopes(db).rules(11, "mrs_job")
            assert db.get(AlarmRule, "R-MRS-JOB-001").resource_type == "dgc_mrs_node"
            assert db.get(AlarmRule, "R-OPERATOR").resource_type == "mrs_job"
        command.downgrade(cfg, "0021")
        with Session(engine) as db:
            assert db.get(Resource, node_id).resource_type == "mrs_job"
            assert db.get(AlarmEvent, alarm_id).fingerprint == old_fp
            assert db.get(EvalState, old_fp).data["hits"] == 5
            assert db.get(InspectionTask, "dgc_mrs_node_status") is None
            assert db.scalar(select(CollectSwitch).where(CollectSwitch.tenant_id == 11,
                                                       CollectSwitch.resource_type == "mrs_job")).enabled
        command.upgrade(cfg, "0022")
    finally:
        engine.dispose()


@pytest.mark.parametrize("scoped,enabled", [(False, False), (True, False), (False, True)])
def test_task_off_and_custom_rules_are_preserved(migration_url, scoped, enabled):
    cfg = config(migration_url)
    command.upgrade(cfg, "0021")
    engine = create_engine(migration_url)
    try:
        seed(engine, scoped=scoped, enabled=enabled, customized=True)
        command.upgrade(cfg, "0022")
        with Session(engine) as db:
            assert db.get(InspectionTask, "mrs_job_status").enabled is enabled
            assert db.get(InspectionTask, "dgc_mrs_node_status").enabled is enabled
            assert db.get(AlarmRule, "R-MRS-JOB-001").resource_type == "mrs_job"
            assert db.get(AlarmRule, "R-MRS-JOB-001").runbook.endswith("operator note")
            if not scoped:
                assert switches(db).enabled(11, "mrs_job")
    finally:
        engine.dispose()


def test_unverifiable_active_alarm_aborts_without_partial_mutation(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0021")
    engine = create_engine(migration_url)
    try:
        node_id, _, alarm_id, _ = seed(engine, broken_fp=True)
        with pytest.raises(RuntimeError, match="无法验证"):
            command.upgrade(cfg, "0022")
        with Session(engine) as db:
            assert db.get(Resource, node_id).resource_type == "mrs_job"
            assert db.get(AlarmEvent, alarm_id).status == "ACKED"
            assert db.get(InspectionTask, "dgc_mrs_node_status") is None
    finally:
        engine.dispose()


def test_new_database_defaults_off_and_bootstrap_does_not_reenable(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0022")
    engine = create_engine(migration_url)
    try:
        with Session(engine) as db:
            ensure_tasks(db)
            assert all(not db.get(InspectionTask, code).enabled for code in ("mrs_cluster_status", "mrs_job_status",
                                                                           "dgc_mrs_node_status"))
            assert not switches(db).enabled(11, "mrs_job")
            assert not switches(db).enabled(11, "dgc_mrs_node")
    finally:
        engine.dispose()


def test_target_resource_conflict_refuses_to_merge_or_delete(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0021")
    engine = create_engine(migration_url)
    try:
        node_id, _, _, _ = seed(engine)
        with Session(engine) as db:
            original = db.get(Resource, node_id)
            db.add(Resource(tenant_id=11, project_ref=111, resource_type="dgc_mrs_node", service="dataarts",
                            resource_id=original.resource_id, name="conflict"))
            db.commit()
        with pytest.raises(RuntimeError, match="目标资源已存在"):
            command.upgrade(cfg, "0022")
        with Session(engine) as db:
            assert len(db.scalars(select(Resource)).all()) == 3
    finally:
        engine.dispose()


@pytest.mark.parametrize("raw,native_on,node_on", [
    ({"rules": [{"effect": "include", "match": {"cluster": ["c1"]}}]}, True, False),
    ({"rules": [{"effect": "include", "match": {"job": ["authorized-job"]}}]}, False, True),
    ({"rules": [{"effect": "invalid", "match": {"cluster": ["c1"]}}]}, False, False),
    ({"rules": "invalid"}, False, False),
])
def test_scope_migration_never_drops_constraints_or_widens_authorization(migration_url, raw, native_on, node_on):
    cfg = config(migration_url)
    command.upgrade(cfg, "0021")
    engine = create_engine(migration_url)
    try:
        seed(engine, scoped=False)
        with Session(engine) as db:
            db.add(SysSetting(key="collect_scope:11:mrs_job", value=raw, updated_by="operator"))
            db.commit()
        command.upgrade(cfg, "0022")
        with Session(engine) as db:
            assert switches(db).enabled(11, "mrs_job") is native_on
            assert switches(db).enabled(11, "dgc_mrs_node") is node_on
            assert db.get(SysSetting, "collect_scope:11:dgc_mrs_node").value == raw
            assert db.get(SysSetting, "collect_scope:11:mrs_job").value == raw
        command.downgrade(cfg, "0021")
        with Session(engine) as db:
            assert switches(db).enabled(11, "mrs_job")
            assert db.get(SysSetting, "collect_scope:11:mrs_job").value == raw
    finally:
        engine.dispose()


def test_global_legacy_scope_and_native_tenant_override_keep_independent_authorization(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0021")
    engine = create_engine(migration_url)
    try:
        seed(engine)
        with Session(engine) as db:
            db.add(SysSetting(key="collect_scope:11:mrs_job", value={"rules": [
                {"effect": "include", "match": {"id": ["c/native"]}}]}, updated_by="operator"))
            db.commit()
        command.upgrade(cfg, "0022")
        with Session(engine) as db:
            assert switches(db).enabled(11, "mrs_job")
            assert not switches(db).enabled(11, "dgc_mrs_node")
            assert not switches(db).enabled(22, "mrs_job")
            assert scopes(db).rules(11, "dgc_mrs_node")[0]["match"] == {"id": ["c/native"]}
    finally:
        engine.dispose()


def test_downgrade_refuses_post_migration_user_edits(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0021")
    engine = create_engine(migration_url)
    try:
        node_id, _, alarm_id, _ = seed(engine)
        command.upgrade(cfg, "0022")
        with Session(engine) as db:
            db.get(Resource, node_id).attrs = {"source": "dataarts_instance_detail", "new": "observation"}
            db.commit()
        with pytest.raises(RuntimeError, match="已变更"):
            command.downgrade(cfg, "0021")
        with Session(engine) as db:
            assert db.get(Resource, node_id).resource_type == "dgc_mrs_node"
            assert db.get(AlarmEvent, alarm_id).status == "ACKED"
    finally:
        engine.dispose()


def test_fresh_alembic_install_still_initializes_every_production_switch_disabled(migration_url, monkeypatch):
    from types import SimpleNamespace

    from app.services import bootstrap as service
    from app.services import collect_switch

    cfg = config(migration_url)
    command.upgrade(cfg, "0022")
    engine = create_engine(migration_url)
    try:
        monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(env="prod"))
        with Session(engine) as db:
            service.bootstrap(db)
            rows = db.scalars(select(CollectSwitch)).all()
            assert {row.resource_type for row in rows if row.tenant_id == 0} == set(collect_switch.SWITCH_TYPES)
            assert all(not row.enabled for row in rows)
            assert all(not task.enabled for task in db.scalars(select(InspectionTask)))
    finally:
        engine.dispose()


def test_migration_preserves_preconfigured_node_scope_switch_and_schedule(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0021")
    engine = create_engine(migration_url)
    node_scope = {"rules": [{"effect": "include", "match": {"workspace": ["ws-authorized"]}}]}
    try:
        seed(engine, scoped=False)
        with Session(engine) as db:
            db.add_all([
                SysSetting(key="collect_scope:11:mrs_job", value={"rules": [
                    {"effect": "include", "match": {"cluster": ["c1"]}}]}, updated_by="operator"),
                SysSetting(key="collect_scope:11:dgc_mrs_node", value=node_scope, updated_by="operator"),
                CollectSwitch(tenant_id=11, resource_type="dgc_mrs_node", enabled=True, updated_by="operator"),
                InspectionTask(code="dgc_mrs_node_status", name="Already split", service="dataarts", interval_sec=991,
                               enabled=False, params={"max_details": 9}),
            ])
            db.commit()
        command.upgrade(cfg, "0022")
        with Session(engine) as db:
            assert switches(db).enabled(11, "dgc_mrs_node")
            assert db.get(SysSetting, "collect_scope:11:dgc_mrs_node").value == node_scope
            task = db.get(InspectionTask, "dgc_mrs_node_status")
            assert not task.enabled and task.interval_sec == 991 and task.params == {"max_details": 9}
    finally:
        engine.dispose()


def test_fresh_install_without_migration_journal_still_refuses_lossy_node_downgrade(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0022")
    engine = create_engine(migration_url)
    try:
        with Session(engine) as db:
            assert db.get(SysSetting, "migration:mrs-split-0022") is None
            node = Resource(resource_type="dgc_mrs_node", service="dataarts", resource_id="ws/job/sql", name="sql",
                            attrs={"source": "dataarts_instance_detail"})
            db.add(node)
            db.commit()
            ref = node.id
        with pytest.raises(RuntimeError, match="已有新节点采集"):
            command.downgrade(cfg, "0021")
        with Session(engine) as db:
            assert db.get(Resource, ref).resource_type == "dgc_mrs_node"
    finally:
        engine.dispose()


def test_fresh_install_initialized_without_reverse_snapshot_refuses_unsafe_downgrade(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0022")
    engine = create_engine(migration_url)
    try:
        with Session(engine) as db:
            ensure_tasks(db)
            db.commit()
        with pytest.raises(RuntimeError, match="已有节点初始化配置"):
            command.downgrade(cfg, "0021")
        with Session(engine) as db:
            assert db.get(InspectionTask, "dgc_mrs_node_status") is not None
    finally:
        engine.dispose()
