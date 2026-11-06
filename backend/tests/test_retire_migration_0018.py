"""迁移 0018：下线 RDS / Kafka / CSS，旧口径 MRS 作业替换为 DataArts 节点口径；规则可逆，自定义规则不覆盖。"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import AlarmEvent, AlarmEventLog, AlarmRule, CollectSwitch, EvalState, Resource, ResourceChange, SysSetting

OLD_001 = {"when": "job_state == 'FAILED' or job_result == 'FAILED'", "severity": "P2", "critical_severity": "P1",
           "value_field": "job_result", "title": "作业执行失败（{job_result}）"}
OLD_003 = {"when": "job_state == 'ACCEPTED' and status_duration_min > 15", "severity": "P3",
           "value_field": "job_state", "title": "作业排队已 {status_duration_min} 分钟"}
MARKER = "system:dataarts-mrs-v1"


def config(url):
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "migrations"))
    cfg.attributes["database_url"] = url
    return cfg


def resource(rtype, rid, **attrs):
    return Resource(service="x", resource_type=rtype, resource_id=rid, name=rid, tenant_id=1, project_ref=1, attrs=attrs)


def alarm(ref, rtype, fingerprint, status="FIRING"):
    return AlarmEvent(tenant_id=1, fingerprint=fingerprint, rule_id="R-X", resource_ref=ref, service="x",
                      resource_type=rtype, resource_name="n", severity="P2", status=status, title="t", check_item="c")


def rule(code, expr, **extra):
    return AlarmRule(id=code, name=code, resource_type="mrs_job", check_item="job_status", kind="state", expr=expr,
                     recover={"when": "job_result == 'SUCCEEDED'"}, updated_by="system", **extra)


def test_retired_data_is_removed_and_mrs_rules_are_replaced_reversibly(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0017")
    engine = create_engine(migration_url)
    try:
        with Session(engine) as db:
            rds = resource("rds_instance", "rds-1")
            kafka = resource("kafka_instance", "kafka-1")
            old_job = resource("mrs_job", "cluster/job", job_id="exec-1")
            new_job = resource("mrs_job", "ws/job/node", source="dataarts_instance_detail", instance_id=7)
            ecs = resource("ecs_server", "ecs-1")
            db.add_all([rds, kafka, old_job, new_job, ecs, CollectSwitch(tenant_id=0, resource_type="css_cluster",
                                                                       enabled=False),
                        CollectSwitch(tenant_id=0, resource_type="ecs_server", enabled=False)])
            db.flush()
            db.add_all([alarm(rds.id, "rds_instance", "fp-rds"), alarm(old_job.id, "mrs_job", "fp-old"),
                        alarm(new_job.id, "mrs_job", "fp-new"), alarm(ecs.id, "ecs_server", "fp-ecs"),
                        alarm(kafka.id, "kafka_instance", "fp-resolved", status="RESOLVED"),
                        ResourceChange(tenant_id=1, resource_ref=rds.id, resource_type="rds_instance", resource_id="rds-1",
                                       name="rds-1", change_type="CREATED", changes=[]),
                        EvalState(fingerprint="fp-rds", data={"_inspection_evidence": {"resource_type": "rds_instance"}}),
                        EvalState(fingerprint="fp-keep", data={"_inspection_evidence": {"resource_type": "ecs_server"}}),
                        rule("R-MRS-JOB-001", OLD_001), rule("R-MRS-JOB-003", OLD_003),
                        AlarmRule(id="R-CUSTOM-MRS", name="custom", resource_type="mrs_job", check_item="job_status",
                                  kind="state", expr={"when": "job_state == 'FAILED'", "severity": "P1"},
                                  recover={}, updated_by="operator")])
            db.commit()
            ids = {"rds": rds.id, "old": old_job.id, "new": new_job.id, "ecs": ecs.id}

        command.upgrade(cfg, "0018")
        with Session(engine) as db:
            kept = {r.resource_id for r in db.scalars(select(Resource))}
            assert kept == {"ws/job/node", "ecs-1"}
            assert {s.resource_type for s in db.scalars(select(CollectSwitch))} == {"ecs_server"}
            assert db.scalars(select(ResourceChange)).all() == []
            assert {e.fingerprint for e in db.scalars(select(EvalState))} == {"fp-keep"}
            status = {a.fingerprint: a.status for a in db.scalars(select(AlarmEvent))}
            assert status == {"fp-rds": "CLOSED", "fp-old": "CLOSED", "fp-new": "FIRING", "fp-ecs": "FIRING",
                              "fp-resolved": "RESOLVED"}
            closed = db.scalars(select(AlarmEventLog).where(AlarmEventLog.action == "CLOSE")).all()
            assert len(closed) == 2 and all(log.operator == "system" and log.to_status == "CLOSED" for log in closed)
            fail = db.get(AlarmRule, "R-MRS-JOB-001")
            assert fail.expr["when"] == "node_status == 'fail'" and fail.recover == {"when": "node_status == 'success'"}
            assert fail.version == 2 and fail.updated_by == MARKER and "logPath" in fail.runbook
            queue = db.get(AlarmRule, "R-MRS-JOB-003")
            assert queue.enabled is False and queue.expr == OLD_003 and queue.version == 2
            assert db.get(AlarmRule, "R-CUSTOM-MRS").expr["when"] == "job_state == 'FAILED'"
            assert db.get(SysSetting, "rule-dataarts-mrs-0018:R-MRS-JOB-001") is not None
            assert ids["ecs"] in {r.id for r in db.scalars(select(Resource))}

        command.downgrade(cfg, "0017")
        with Session(engine) as db:
            fail, queue = db.get(AlarmRule, "R-MRS-JOB-001"), db.get(AlarmRule, "R-MRS-JOB-003")
            assert fail.expr == OLD_001 and fail.recover == {"when": "job_result == 'SUCCEEDED'"}
            assert fail.version == 1 and fail.updated_by == "system"
            assert queue.enabled is True and queue.version == 1
            assert db.scalars(select(SysSetting).where(SysSetting.key.startswith("rule-dataarts-mrs-0018:"))).all() == []
        command.upgrade(cfg, "0018")
    finally:
        engine.dispose()


def test_customized_or_already_new_rules_are_not_rewritten(migration_url):
    cfg = config(migration_url)
    command.upgrade(cfg, "0017")
    engine = create_engine(migration_url)
    try:
        with Session(engine) as db:
            edited = rule("R-MRS-JOB-001", {**OLD_001, "severity": "P1"}, version=3)
            edited.updated_by = "operator"
            db.add_all([edited, rule("R-MRS-JOB-003", {**OLD_003, "severity": "P2"})])
            db.commit()
        command.upgrade(cfg, "0018")
        with Session(engine) as db:
            assert db.get(AlarmRule, "R-MRS-JOB-001").expr["severity"] == "P1"
            assert db.get(AlarmRule, "R-MRS-JOB-001").version == 3
            assert db.get(AlarmRule, "R-MRS-JOB-003").enabled is True
            assert db.scalars(select(SysSetting).where(SysSetting.key.startswith("rule-dataarts-mrs-0018:"))).all() == []
    finally:
        engine.dispose()
