"""交主代理处理的共享流水线/规则回归；本文件不修改共享实现，不调用云接口。"""
from datetime import timedelta
from unittest.mock import Mock

from sqlalchemy import select

from app.collectors.base import Observation, RunContext
from app.models import AlarmEvent, AlarmRule
from app.models.base import utcnow
from app.rules.evaluator import evaluate_rule
from app.rules.loader import load_yaml_rules
from app.rules.model import Rule
from app.services.pipeline import process_observations


def context():
    return RunContext(client=Mock(), account="synthetic", project_id="synthetic-project")


def rule(db, code, kind, check, expression):
    db.add(AlarmRule(id=code, name=code, resource_type=kind, check_item=check, kind="state",
                     expr={"when": expression, "severity": "P3"}, recover={"when": "False"}))
    db.commit()


def alarms(db):
    return db.scalars(select(AlarmEvent)).all()


def test_mrs_new_instance_does_not_inherit_previous_instances_waiting_duration(mem_db):
    rule(mem_db, "REVIEW-MRS-DURATION", "mrs_job", "job_status",
         "node_status == 'waiting' and status_duration_min > 15")
    first = utcnow()
    ctx = context()

    def job(execution):
        return Observation("mrs_job", "ws/same-name/node", "same-name", "job_status",
                           {"node_status": "waiting"}, status_raw="waiting", attrs={"instance_id": execution})

    process_observations(mem_db, "mrs", [job("old-instance")], ctx, first)
    process_observations(mem_db, "mrs", [job("new-instance")], ctx, first + timedelta(minutes=20))
    assert alarms(mem_db) == []


def test_realtime_new_node_does_not_inherit_parent_job_duration(mem_db):
    rule(mem_db, "REVIEW-NODE-DURATION", "dgc_realtime_job", "node_status",
         "node_status == 'STARTING' and status_duration_min > 10")
    first = utcnow()
    ctx = context()
    parent = Observation("dgc_realtime_job", "workspace/stream", "stream", "job_status",
                         {"status": "NORMAL"}, status_raw="NORMAL")
    process_observations(mem_db, "dataarts", [parent], ctx, first)
    node = Observation("dgc_realtime_job", "workspace/stream", "stream", "node_status",
                       {"job_status": "NORMAL", "node_status": "STARTING"}, sub_object="new-node")
    process_observations(mem_db, "dataarts", [parent, node], ctx, first + timedelta(minutes=30))
    assert alarms(mem_db) == []


def test_explicit_observation_duration_is_not_overwritten_by_parent_snapshot(mem_db):
    rule(mem_db, "REVIEW-EXPLICIT-DURATION", "dgc_realtime_job", "node_status",
         "node_status == 'STARTING' and status_duration_min > 10")
    parent = Observation("dgc_realtime_job", "workspace/stream", "stream", "job_status",
                         {"status": "NORMAL"}, status_raw="NORMAL")
    node = Observation("dgc_realtime_job", "workspace/stream", "stream", "node_status",
                       {"job_status": "NORMAL", "node_status": "STARTING", "status_duration_min": 20},
                       sub_object="node")
    process_observations(mem_db, "dataarts", [parent, node], context(), utcnow())
    assert len(alarms(mem_db)) == 1


def test_mrs_unknown_state_is_not_positive_recovery_evidence():
    raw = next(item for item in load_yaml_rules() if item["id"] == "R-MRS-JOB-001")
    current = Rule(**raw)
    decision = evaluate_rule(current, {"node_status": "FUTURE_STATE"}, {}, "P2")
    assert decision.action != "RESOLVE"


def test_realtime_failure_hits_do_not_bridge_an_explicit_missing_sample(mem_db):
    rule(mem_db, "REVIEW-MISSING-HITS", "dgc_realtime_job", "job_status", "status == 'EXCEPTION'")
    saved = mem_db.get(AlarmRule, "REVIEW-MISSING-HITS")
    saved.for_count = 2
    mem_db.commit()
    first = utcnow()
    ctx = context()
    ctx.params.update(_task_code="dgc_realtime_status", _inspection_stale_after_sec=900)
    bad = Observation("dgc_realtime_job", "workspace/stream", "stream", "job_status",
                      {"status": "EXCEPTION"}, status_raw="EXCEPTION")
    process_observations(mem_db, "dataarts", [bad], ctx, first)
    process_observations(mem_db, "dataarts", [], ctx, first + timedelta(minutes=5))
    process_observations(mem_db, "dataarts", [bad], ctx, first + timedelta(minutes=10))
    assert alarms(mem_db) == []


def test_recovery_confirmation_does_not_bridge_an_explicit_missing_sample(mem_db):
    rule(mem_db, "REVIEW-MISSING-RECOVERY", "dgc_realtime_job", "job_status", "status == 'EXCEPTION'")
    saved = mem_db.get(AlarmRule, "REVIEW-MISSING-RECOVERY")
    saved.recover = {"when": "status == 'NORMAL'", "for_count": 2}
    mem_db.commit()
    first = utcnow()
    ctx = context()
    ctx.params.update(_task_code="dgc_realtime_status", _inspection_stale_after_sec=900)
    bad = Observation("dgc_realtime_job", "workspace/stream", "stream", "job_status",
                      {"status": "EXCEPTION"}, status_raw="EXCEPTION")
    good = Observation("dgc_realtime_job", "workspace/stream", "stream", "job_status",
                       {"status": "NORMAL"}, status_raw="NORMAL")
    process_observations(mem_db, "dataarts", [bad], ctx, first)
    process_observations(mem_db, "dataarts", [good], ctx, first + timedelta(minutes=1))
    process_observations(mem_db, "dataarts", [], ctx, first + timedelta(minutes=6))
    process_observations(mem_db, "dataarts", [good], ctx, first + timedelta(minutes=11))
    assert alarms(mem_db)[0].status == "FIRING"


def test_node_failure_hits_do_not_bridge_missing_node_under_an_observed_parent(mem_db):
    rule(mem_db, "REVIEW-MISSING-NODE-HITS", "dgc_realtime_job", "node_status", "node_status == 'EXCEPTION'")
    saved = mem_db.get(AlarmRule, "REVIEW-MISSING-NODE-HITS")
    saved.for_count = 2
    mem_db.commit()
    first = utcnow()
    ctx = context()
    ctx.params.update(_task_code="dgc_realtime_status", _inspection_stale_after_sec=900)
    parent = Observation("dgc_realtime_job", "workspace/stream", "stream", "job_status",
                         {"status": "NORMAL"}, status_raw="NORMAL")
    node = Observation("dgc_realtime_job", "workspace/stream", "stream", "node_status",
                       {"job_status": "NORMAL", "node_status": "EXCEPTION"}, sub_object="node-a")
    process_observations(mem_db, "dataarts", [parent, node], ctx, first)
    process_observations(mem_db, "dataarts", [parent], ctx, first + timedelta(minutes=5))
    process_observations(mem_db, "dataarts", [parent, node], ctx, first + timedelta(minutes=10))
    assert alarms(mem_db) == []
