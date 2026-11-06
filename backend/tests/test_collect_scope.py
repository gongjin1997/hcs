"""采集范围：规则保存与同步、范围外资源的告警关闭、流水线与采集器裁剪、接口权限与审计。

云端调用全部使用进程内替身；匹配引擎本身见 test_collect_scope_engine.py。
"""
from copy import deepcopy
from datetime import timedelta
from urllib.parse import unquote
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.v1.system import router
from app.collectors import dataarts
from app.collectors.base import Observation, RunContext
from app.collectors.dcs import DcsCollector
from app.collectors.gaussdb import GaussDBCollector
from app.collectors.mrs import MrsJobCollector
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.core.response import BizError
from app.k8s.collector import PodEventCollector, _Run
from app.models import (
    AlarmEvent,
    AlarmEventLog,
    AuditLog,
    HcsAccount,
    InspectionRun,
    PodEvent,
    PodState,
    Resource,
    SysSetting,
)
from app.models.base import utcnow
from app.models.pod import SCOPE_META
from app.schemas.collection_policy import CollectionPolicy
from app.services import collect_scope, collection_policy
from app.services.catalog import RESOURCE_TYPES
from app.services.inspection_health import inspection_state
from app.services.pipeline import process_observations, run_task

from .test_cce_hcs851_contract import NOW as K8S_NOW
from .test_cce_hcs851_contract import Client as K8sClient
from .test_cce_hcs851_contract import control_cluster, page
from .test_mrs_hcs851_contract import NOW as MRS_NOW
from .test_mrs_hcs851_contract import DataArtsClient, inst, job_ctx, node
from .test_pipeline import _client as mock_client
from .test_pipeline import _setup as pipeline_setup


def exclude(**match):
    return {"effect": "exclude", "match": {k: v if isinstance(v, list) else [v] for k, v in match.items()}}


def include(**match):
    return {"effect": "include", "match": {k: v if isinstance(v, list) else [v] for k, v in match.items()}}


def scopes(**by_tenant_type):
    """scopes(t11__mrs_job=[...]) → 内存中的采集范围（租户 11，MRS 作业）；t0 为全局。"""
    result = collect_scope.Scopes()
    for key, rules in by_tenant_type.items():
        tenant_key, rtype = key.split("__", 1)
        result.raw[(int(tenant_key.removeprefix("t")), rtype)] = {"rules": rules}
    return result


def tenant(db, tid=11):
    row = HcsAccount(id=tid, code=f"tenant-{tid}", name=f"tenant-{tid}", iam_endpoint="https://example.invalid",
                     domain_name="example")
    db.add(row)
    db.commit()
    return row


def add_resource(db, rtype, rid, name, *, tid=11, project=101, **kwargs):
    row = Resource(tenant_id=tid, project_ref=project, service="x", resource_type=rtype, resource_id=rid, name=name,
                   health=kwargs.pop("health", "OK"), **kwargs)
    db.add(row)
    db.flush()
    return row


def add_alarm(db, resource, *, status="FIRING", rule_id="R-TEST"):
    alarm = AlarmEvent(tenant_id=resource.tenant_id, fingerprint=uuid4().hex, rule_id=rule_id, resource_ref=resource.id,
                       service="x", resource_type=resource.resource_type, resource_name=resource.name,
                       check_item="instance_status", severity="P2", status=status, title="异常")
    db.add(alarm)
    db.flush()
    return alarm


def states(db, rtype=None):
    """名称 → (是否范围外, 健康)。会话使用 expire_on_commit=False，批量更新后重新读取数据库。"""
    query = select(Resource.name, Resource.scope_excluded, Resource.health)
    if rtype:
        query = query.where(Resource.resource_type == rtype)
    db.expire_all()
    return {name: (bool(excluded), health) for name, excluded, health in db.execute(query)}


def flags(db, rtype=None):
    return {name: excluded for name, (excluded, _health) in states(db, rtype).items()}


# ---------------------------------------------------------------- 保存、标记、告警
def test_saving_a_scope_flags_resources_closes_their_alarms_and_restores_on_removal(mem_db):
    tenant(mem_db)
    keep = add_resource(mem_db, "dcs_instance", "i-1", "redis-prod-1")
    drop = add_resource(mem_db, "dcs_instance", "i-2", "redis-cache-1")
    add_resource(mem_db, "gaussdb_instance", "g-1", "redis-cache-gauss")
    kept_alarm, dropped_alarm = add_alarm(mem_db, keep), add_alarm(mem_db, drop)
    mem_db.commit()

    result = collect_scope.save(mem_db, 11, "dcs_instance", [exclude(name="redis-cache-*")], "tester")
    mem_db.commit()
    assert result["changed"] and result["before"] is None
    assert result["after"] == [exclude(name="redis-cache-*")]
    assert (result["excluded"], result["restored"], result["closed_alarms"]) == (1, 0, 1)
    assert states(mem_db) == {"redis-prod-1": (False, "OK"), "redis-cache-1": (True, "UNKNOWN"),
                              "redis-cache-gauss": (False, "OK")}
    mem_db.refresh(kept_alarm)
    mem_db.refresh(dropped_alarm)
    assert dropped_alarm.status == "CLOSED" and kept_alarm.status == "FIRING"
    log = mem_db.scalar(select(AlarmEventLog).where(AlarmEventLog.alarm_id == dropped_alarm.id))
    assert log.action == "CLOSE" and log.operator == "tester" and "非指标恢复" in log.note

    again = collect_scope.save(mem_db, 11, "dcs_instance", [exclude(name="redis-cache-*")], "tester")
    assert not again["changed"] and again["excluded"] == 0                      # 重复保存不再关闭任何告警

    back = collect_scope.save(mem_db, 11, "dcs_instance", None, "tester")        # 改为跟随全局（全局无设置）
    mem_db.commit()
    assert back["changed"] and (back["excluded"], back["restored"]) == (0, 1)
    assert flags(mem_db, "dcs_instance") == {"redis-prod-1": False, "redis-cache-1": False}
    mem_db.refresh(dropped_alarm)
    assert dropped_alarm.status == "CLOSED"                                      # 已关闭的告警不会自动重开
    assert mem_db.scalar(select(SysSetting).where(SysSetting.key == "collect_scope:11:dcs_instance")) is None


def test_tenant_setting_overrides_global_and_empty_tenant_rules_mean_everything(mem_db):
    tenant(mem_db, 11)
    tenant(mem_db, 22)
    add_resource(mem_db, "cce_workload", "c/ns-a/ReplicaSet/w-1", "ns-a/w-1", tid=11,
                 attrs={"cluster": "c", "cluster_id": "c", "namespace": "ns-a", "workload": "w-1"})
    add_resource(mem_db, "cce_workload", "c/ns-a/ReplicaSet/w-2", "ns-a/w-2", tid=22,
                 attrs={"cluster": "c", "cluster_id": "c", "namespace": "ns-a", "workload": "w-2"})
    mem_db.commit()
    collect_scope.save(mem_db, 0, "cce_workload", [exclude(namespace="ns-*")], "admin")
    mem_db.commit()
    assert flags(mem_db) == {"ns-a/w-1": True, "ns-a/w-2": True}              # 两个租户都跟随全局

    collect_scope.save(mem_db, 22, "cce_workload", [], "admin")                  # 租户 22 明确“不限制”
    mem_db.commit()
    assert flags(mem_db) == {"ns-a/w-1": True, "ns-a/w-2": False}
    scopes_now = collect_scope.load(mem_db)
    assert scopes_now.source(11, "cce_workload") == "global" and scopes_now.source(22, "cce_workload") == "tenant"

    collect_scope.save(mem_db, 0, "cce_workload", [], "admin")                   # 清空全局 = 全部采集，设置被删除
    mem_db.commit()
    assert flags(mem_db) == {"ns-a/w-1": False, "ns-a/w-2": False}
    assert mem_db.scalar(select(SysSetting).where(SysSetting.key == "collect_scope:0:cce_workload")) is None


def test_global_cannot_follow_global_and_unknown_tenant_or_type_are_rejected(mem_db):
    tenant(mem_db)
    with pytest.raises(BizError):
        collect_scope.save(mem_db, 0, "dcs_instance", None, "admin")
    with pytest.raises(BizError):
        collect_scope.save(mem_db, 99, "dcs_instance", [], "admin")
    with pytest.raises(BizError):
        collect_scope.save(mem_db, 11, "rds_instance", [], "admin")                # 已下线的类型


def test_ledger_dimensions_follow_manual_edits_and_unassigned_resources_do_not_match(mem_db):
    tenant(mem_db)
    paid = add_resource(mem_db, "dcs_instance", "i-1", "r1", business="pay-core", env_type="PROD")
    free = add_resource(mem_db, "dcs_instance", "i-2", "r2")
    mem_db.commit()
    collect_scope.save(mem_db, 11, "dcs_instance", [include(business="pay*", env="PROD")], "admin")
    mem_db.commit()
    assert flags(mem_db) == {"r1": False, "r2": True}

    mem_db.refresh(free)
    free.business, free.env_type = "pay-risk", "PROD"                             # 页面修改负责业务后重新判定
    assert collect_scope.refresh_resource(mem_db, free, operator="u") is True
    mem_db.commit()
    assert flags(mem_db) == {"r1": False, "r2": False}
    mem_db.refresh(paid)
    paid.env_type = "TEST"
    assert collect_scope.refresh_resource(mem_db, paid, operator="u") is True
    assert paid.scope_excluded is True and collect_scope.refresh_resource(mem_db, paid) is False


def test_candidates_and_preview_describe_the_effect_before_saving(mem_db):
    tenant(mem_db)
    tenant(mem_db, 22)
    for i, ns in enumerate(("prod", "prod", "test-1", "test-2")):
        add_resource(mem_db, "cce_workload", f"c/{ns}/ReplicaSet/w{i}", f"{ns}/w{i}",
                     attrs={"cluster": "cce-a", "cluster_id": "c", "namespace": ns, "workload": f"w{i}"})
    add_resource(mem_db, "cce_workload", "c/other/ReplicaSet/x", "other/x", tid=22,
                 attrs={"cluster": "cce-b", "cluster_id": "c2", "namespace": "other", "workload": "x"})
    drop_alarm = add_alarm(mem_db, mem_db.scalar(select(Resource).where(Resource.name == "test-1/w2")))
    mem_db.commit()

    assert collect_scope.candidates(mem_db, 11, "cce_workload", "namespace") == [
        {"value": "prod", "count": 2}, {"value": "test-1", "count": 1}, {"value": "test-2", "count": 1}]
    assert [c["value"] for c in collect_scope.candidates(mem_db, 11, "cce_workload", "namespace", "TEST")] == [
        "test-1", "test-2"]
    assert collect_scope.candidates(mem_db, 11, "cce_workload", "cluster") == [{"value": "cce-a", "count": 4}]
    assert {c["value"] for c in collect_scope.candidates(mem_db, 0, "cce_workload", "namespace")} == {
        "prod", "test-1", "test-2", "other"}
    with pytest.raises(BizError):
        collect_scope.candidates(mem_db, 11, "cce_workload", "business")           # 该类型没有这个维度

    result = collect_scope.preview(mem_db, 11, "cce_workload", [exclude(namespace="test-*")])
    assert (result["total"], result["included"], result["excluded"]) == (4, 2, 2)
    assert (result["newly_excluded"], result["newly_included"], result["closing_alarms"]) == (2, 0, 1)
    assert sorted(result["samples"]["excluded"]) == ["test-1/w2", "test-2/w3"]
    mem_db.refresh(drop_alarm)
    assert drop_alarm.status == "FIRING"                                            # 预览不改任何数据
    assert flags(mem_db, "cce_workload")["test-1/w2"] is False
    assert collect_scope.preview(mem_db, 11, "cce_workload", [])["excluded"] == 0


def test_flag_sync_for_a_large_scope_only_touches_changed_rows(mem_db):
    tenant(mem_db)
    for i in range(1200):
        add_resource(mem_db, "dcs_instance", f"i-{i}", f"redis-{'tmp' if i % 2 else 'prod'}-{i}")
    mem_db.commit()
    result = collect_scope.save(mem_db, 11, "dcs_instance", [exclude(name="redis-tmp-*")], "admin")
    mem_db.commit()
    assert result["excluded"] == 600 and result["restored"] == 0
    assert collect_scope.sync_flags(mem_db, 11, ["dcs_instance"]) == {"excluded": 0, "restored": 0, "closed_alarms": 0}


# ---------------------------------------------------------------- 巡检语义
def test_out_of_scope_resources_are_not_inspected_monitored_or_reported_missing(mem_db):
    tenant(mem_db)
    row = add_resource(mem_db, "ecs_server", "e-1", "web-1", attrs={"inspection_monitoring": {
        "evaluated_at": utcnow().isoformat(), "checked_rules": 2, "unavailable_rules": 0, "stale_after_sec": 900}})
    mem_db.commit()
    assert inspection_state(row) == "FRESH"
    collect_scope.save(mem_db, 11, "ecs_server", [exclude(name="web-*")], "admin")
    mem_db.commit()
    mem_db.refresh(row)
    assert row.scope_excluded and inspection_state(row) == "DISABLED"
    assert collection_policy.resource_included(row, CollectionPolicy()) is False
    assert collection_policy.select_resources(mem_db, 11, 101, CollectionPolicy()) == []
    collect_scope.save(mem_db, 11, "ecs_server", [], "admin")
    mem_db.commit()
    assert [r.id for r in collection_policy.select_resources(mem_db, 11, 101, CollectionPolicy())] == [row.id]


def dgc(name, workspace, **attrs):
    return Observation("dgc_batch_job", f"{workspace}/{name}", name, "instance_status", {"last_status": "success"},
                       status_raw="success", attrs={"workspace": workspace, **attrs})


def test_pipeline_drops_out_of_scope_objects_marks_existing_rows_and_never_creates_new_ones(mem_db):
    tenant(mem_db)
    old = add_resource(mem_db, "dgc_batch_job", "ws-9/old", "old", attrs={"workspace": "ws-9"})
    alarm = add_alarm(mem_db, old)
    mem_db.commit()
    collect_scope.save(mem_db, 11, "dgc_batch_job", [exclude(workspace="ws-9")], "admin")
    mem_db.commit()
    mem_db.refresh(alarm)
    assert alarm.status == "CLOSED"

    ctx = RunContext(client=None, account="a", project_id="p", tenant_id=11, project_ref=101)
    process_observations(mem_db, "dataarts", [dgc("etl", "ws-1"), dgc("old", "ws-9"), dgc("new", "ws-9")], ctx, utcnow())
    assert ctx.stats["out_of_scope"] == 2
    assert flags(mem_db, "dgc_batch_job") == {"etl": False, "old": True}              # "new" 没有创建记录
    stale = mem_db.scalar(select(Resource).where(Resource.name == "old"))
    assert "inspection_monitoring" not in (stale.attrs or {})                      # 没有被评估

    # 范围放开后同一对象重新进入巡检，标记自动撤销
    collect_scope.save(mem_db, 11, "dgc_batch_job", [], "admin")
    mem_db.commit()
    ctx = RunContext(client=None, account="a", project_id="p", tenant_id=11, project_ref=101)
    process_observations(mem_db, "dataarts", [dgc("old", "ws-9")], ctx, utcnow())
    assert flags(mem_db, "dgc_batch_job") == {"etl": False, "old": False}


def test_pipeline_without_loaded_scopes_reads_them_from_the_database(mem_db):
    tenant(mem_db)
    collect_scope.save(mem_db, 0, "dgc_batch_job", [exclude(job="tmp_*")], "admin")
    mem_db.commit()
    ctx = RunContext(client=None, account="a", project_id="p", tenant_id=11, project_ref=101)
    assert ctx.scopes is None
    process_observations(mem_db, "dataarts", [dgc("tmp_a", "ws-1"), dgc("real", "ws-1")], ctx, utcnow())
    assert set(flags(mem_db, "dgc_batch_job")) == {"real"}


def test_dcs_end_to_end_exclusion_closes_alarms_and_restoring_the_scope_resumes_alerting(mem_db, monkeypatch):
    from mock_hcs import inventory

    pipeline_setup(mem_db)
    monkeypatch.setitem(inventory.find("dcs", "redis-mq-01"), "status", "ERROR")
    client = mock_client()
    for _ in range(3):
        assert run_task(mem_db, "dcs_status", client=client).status == "SUCCESS"

    def active(*names):
        return mem_db.scalar(select(func.count()).select_from(AlarmEvent).where(
            AlarmEvent.resource_name.in_(names), AlarmEvent.status.in_(("FIRING", "ACKED", "SUPPRESSED"))))

    assert active("redis-mq-01") >= 1 and active("redis-cache-02") >= 1 and active("redis-prod-01") >= 1
    inspected = mem_db.scalar(select(Resource).where(Resource.name == "redis-cache-02"))
    evidence = deepcopy(inspected.attrs["inspection_monitoring"])
    assert evidence["checked_rules"] > 0

    result = collect_scope.save(mem_db, 0, "dcs_instance", [exclude(name=["redis-mq-*", "redis-cache-*"])], "admin")
    mem_db.commit()
    assert result["excluded"] == 2 and result["closed_alarms"] >= 2
    assert active("redis-mq-01", "redis-cache-02") == 0 and active("redis-prod-01") >= 1

    run = run_task(mem_db, "dcs_status", client=client)
    assert run.status == "SUCCESS" and run.resource_total == 4                      # 6 个实例，2 个在范围外
    assert active("redis-mq-01", "redis-cache-02") == 0
    mem_db.expire_all()
    inspected = mem_db.scalar(select(Resource).where(Resource.name == "redis-cache-02"))
    assert inspected.scope_excluded and inspected.attrs["inspection_monitoring"] == evidence   # 没有被记为缺采

    collect_scope.save(mem_db, 0, "dcs_instance", [], "admin")
    mem_db.commit()
    for _ in range(3):
        assert run_task(mem_db, "dcs_status", client=client).status == "SUCCESS"
    assert active("redis-mq-01") >= 1
    mem_db.expire_all()
    assert not mem_db.scalar(select(Resource).where(Resource.name == "redis-mq-01")).scope_excluded
    assert mem_db.scalar(select(InspectionRun).order_by(InspectionRun.id.desc())).status == "SUCCESS"


# ---------------------------------------------------------------- 采集器：调用云接口之前裁剪
class DataArtsJobs:
    """两个工作空间，各有若干实时作业；记录所有请求。"""

    JOBS = {"ws-1": ["etl_a", "tmp_b"], "ws-2": ["other"]}

    def __init__(self):
        self.calls = []

    def get(self, service, path, **kwargs):
        workspace = (kwargs.get("headers") or {}).get("workspace")
        self.calls.append((path, workspace))
        if path == "/v1/{project_id}/jobs":
            names = self.JOBS[workspace]
            return {"total": len(names), "jobs": [{"name": n, "jobType": "REAL_TIME"} for n in names]}
        name = unquote(path.split("/jobs/", 1)[1].split("/status", 1)[0])
        return {"name": name, "status": "NORMAL", "nodes": []}


def realtime_ctx(client, **scope_rules):
    ctx = RunContext(client=client, account="a", project_id="p", workspaces=["ws-1", "ws-2"], tenant_id=11,
                     project_ref=101)
    ctx.scopes = scopes(**scope_rules)
    return ctx


def test_realtime_collector_skips_excluded_workspaces_and_never_queries_excluded_jobs():
    client = DataArtsJobs()
    rows = dataarts.DataArtsRealtimeCollector().collect(realtime_ctx(
        client, t11__dgc_realtime_job=[exclude(workspace="ws-2"), exclude(workspace="ws-1", job="tmp_*")]))
    assert [r.resource_id for r in rows] == ["ws-1/etl_a"]
    assert [c for c in client.calls if c[1] == "ws-2"] == []                          # 整个工作空间没有任何请求
    assert not any("tmp_b" in path for path, _ in client.calls)                       # 范围外作业没有查询状态


def test_realtime_collector_without_a_scope_reads_everything_and_other_tenants_are_unaffected():
    client = DataArtsJobs()
    rows = dataarts.DataArtsRealtimeCollector().collect(realtime_ctx(
        client, t22__dgc_realtime_job=[exclude(workspace="ws-1")]))
    assert {r.resource_id for r in rows} == {"ws-1/etl_a", "ws-1/tmp_b", "ws-2/other"}


def test_mrs_job_collector_skips_excluded_jobs_before_detail_queries_and_filters_nodes(monkeypatch):
    monkeypatch.setattr("app.collectors.mrs.time.time", lambda: MRS_NOW / 1000)
    client = DataArtsClient([inst("job-a", 1), inst("job-tmp", 2)],
                            {("job-a", 1): [node("spark"), node("scratch")], ("job-tmp", 2): [node("spark")]})
    ctx = job_ctx(client)
    ctx.scopes = scopes(t11__mrs_job=[exclude(job="job-tmp*"), exclude(node="scratch")])
    rows = MrsJobCollector().collect(ctx)
    assert [r.resource_id for r in rows] == ["ws-1/job-a/spark"]
    assert client.details() == ["/v1/{project_id}/jobs/job-a/instances/1"]            # job-tmp 的详情没有请求


def test_dcs_and_gaussdb_collectors_trim_instances_before_statistics_and_volume_calls():
    calls = []

    class Dcs:
        def get(self, service, path, **kwargs):
            calls.append(path)
            if path.endswith("/statistic"):
                return {"statistics": [{"instance_id": "one", "used_memory": 10, "max_memory": 100}]}
            return {"instance_num": 2, "instances": [
                {"instance_id": "one", "name": "redis-prod", "status": "RUNNING", "used_memory": 10, "max_memory": 100},
                {"instance_id": "two", "name": "redis-tmp", "status": "RUNNING", "used_memory": 10, "max_memory": 100}]}

    ctx = RunContext(client=Dcs(), account="a", project_id="p", tenant_id=11)
    ctx.scopes = scopes(t11__dcs_instance=[exclude(name="redis-tmp")])
    assert {r.resource_id for r in DcsCollector().collect(ctx)} == {"one"}
    calls.clear()
    ctx.scopes = scopes(t11__dcs_instance=[exclude(name="redis-*")])                  # 全部排除：不再请求统计接口
    assert DcsCollector().collect(ctx) == [] and calls == ["/v2/{project_id}/instances"]

    gauss_calls = []

    class Gauss:
        def get(self, service, path, *, params=None):
            gauss_calls.append(path)
            if path.endswith("/instances"):
                return {"total_count": 2, "instances": [
                    {"id": "g-1", "name": "prod-db", "status": "ACTIVE", "volume": {"size": 9999},
                     "flavor_info": {"mem": 8888}, "nodes": []},
                    {"id": "g-2", "name": "scratch-db", "status": "ACTIVE", "volume": {"size": 9999},
                     "flavor_info": {"mem": 8888}, "nodes": []}]}
            return {"used": "25", "total": "200"}

    ctx = RunContext(client=Gauss(), account="a", project_id="p", tenant_id=11)
    ctx.scopes = scopes(t0__gaussdb_instance=[include(id="g-1")])                      # 全局范围，所有租户适用
    assert {r.resource_id for r in GaussDBCollector().collect(ctx)} == {"g-1"}
    assert [p for p in gauss_calls if p.endswith("volume-usage")] == [
        "/gaussdb/v3/{project_id}/instances/g-1/volume-usage"]


# ---------------------------------------------------------------- CCE：集群 / 命名空间 / 工作负载
def pod(namespace, name, owner):
    return {"metadata": {"uid": f"uid-{namespace}-{name}", "name": name, "namespace": namespace,
                         "ownerReferences": [{"kind": "ReplicaSet", "name": owner, "controller": True}]},
            "spec": {"nodeName": "node-1", "containers": [{"name": "c"}]},
            "status": {"phase": "Running", "podIP": "10.0.0.1", "conditions": [{"type": "Ready", "status": "True"}],
                       "containerStatuses": [{"name": "c", "ready": True, "restartCount": 0,
                                              "state": {"running": {"startedAt": K8S_NOW.isoformat()}}}]}}


def cce_context(mem_db, pods, **scope_rules):
    def fetch(service, path, kwargs):
        if service == "cce":
            return {"items": [control_cluster()]}
        return page(pods) if path.endswith("/pods") else page()

    ctx = RunContext(client=K8sClient(fetch), account="synthetic", project_id="project-a", db=mem_db, tenant_id=11,
                     project_ref=111)
    ctx.scopes = scopes(**scope_rules)
    return ctx


PODS = [pod("prod", "web-1", "web-5d"), pod("prod", "job-1", "batch-7f"), pod("test-1", "web-2", "web-9a"),
        pod("kube-public", "sys-1", "sys-1a")]


def test_cce_collector_skips_excluded_namespaces_and_workloads_without_recording_them(mem_db):
    ctx = cce_context(mem_db, PODS, t11__cce_workload=[exclude(namespace="test-*"), exclude(workload="batch-*")])
    rows = PodEventCollector().collect(ctx)
    assert {r.resource_id for r in rows} == {"cluster-a/prod/ReplicaSet/web-5d"}
    mem_db.flush()
    assert {s.pod for s in mem_db.scalars(select(PodState))} == {"web-1"}


def test_cce_collector_without_scope_keeps_the_default_system_namespace_exclusion(mem_db):
    rows = PodEventCollector().collect(cce_context(mem_db, PODS))
    assert {r.resource_id.split("/")[1] for r in rows} == {"prod", "test-1"}        # kube-public 仍默认屏蔽


def test_cce_include_rule_for_one_cluster_skips_every_other_cluster_before_listing_pods(mem_db):
    ctx = cce_context(mem_db, PODS, t11__cce_workload=[include(cluster="another-cluster")])
    assert PodEventCollector().collect(ctx) == []
    assert [r for r in ctx.client.requests if r[1].endswith("/pods")] == []         # 没有列出任何 Pod
    ctx = cce_context(mem_db, PODS, t11__cce_workload=[include(cluster=["cluster-a"], namespace="prod")])
    assert {r.resource_id.split("/")[1] for r in PodEventCollector().collect(ctx)} == {"prod"}


def test_cce_out_of_scope_workloads_are_not_marked_deleted_when_they_stop_being_listed(mem_db):
    hidden = add_resource(mem_db, "cce_workload", "cluster-a/test-1/ReplicaSet/old", "test-1/old", tid=11, project=111,
                          attrs={"cluster": "cce-a", "cluster_id": "cluster-a", "namespace": "test-1",
                                 "workload": "old"})
    gone = add_resource(mem_db, "cce_workload", "cluster-a/prod/ReplicaSet/gone", "prod/gone", tid=11, project=111,
                        attrs={"cluster": "cce-a", "cluster_id": "cluster-a", "namespace": "prod", "workload": "gone"})
    PodEventCollector().collect(cce_context(mem_db, PODS, t11__cce_workload=[exclude(namespace="test-*")]))
    mem_db.flush()
    assert hidden.lifecycle == "ACTIVE" and gone.lifecycle == "DELETED"


def test_cce_events_of_excluded_namespaces_and_pods_are_not_recorded(mem_db):
    ctx = cce_context(mem_db, [], t11__cce_workload=[exclude(namespace="test-*"), exclude(workload="web-9*")])
    run = _Run(ctx, K8S_NOW)
    run.out_pods.add(("cluster-a", "prod", "batch-pod"))
    stamp = (K8S_NOW - timedelta(seconds=5)).isoformat()

    def event(uid, namespace, kind, name):
        return {"metadata": {"uid": uid, "namespace": namespace},
                "involvedObject": {"kind": kind, "name": name, "namespace": namespace},
                "type": "Warning", "reason": "BackPressure", "message": "backpressure detected",
                "firstTimestamp": stamp, "lastTimestamp": stamp, "count": 1}

    events = [event("e1", "test-1", "Pod", "p"), event("e2", "prod", "Pod", "batch-pod"),
              event("e3", "prod", "ReplicaSet", "web-9a"), event("e4", "prod", "Pod", "web-1"),
              {**event("e5", "", "Node", "node-1"), "involvedObject": {"kind": "Node", "name": "node-1"}}]
    cluster = ("cluster-a", "cce-a")
    kept = [e["metadata"]["uid"] for e in events
            if not run._event_out(cluster, e["involvedObject"].get("namespace", ""), e["involvedObject"])]
    assert kept == ["e4", "e5"]                                                       # Node 事件不受命名空间规则影响
    run._k8s_events(cluster, events, {})
    mem_db.flush()
    recorded = {(e.detail or {}).get(SCOPE_META, {}).get("dedup") for e in mem_db.scalars(select(PodEvent))}
    assert recorded <= {"k8s:e4", "k8s:e5"} and "k8s:e1" not in recorded


# ---------------------------------------------------------------- 接口
@pytest.fixture
def scope_api(mem_db):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    user = CurrentUser(id=1, username="tester", display_name="Tester", tenant_ids=None,
                       permissions=["collect:manage", "task:view"])
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: user

    @app.exception_handler(BizError)
    async def error(_request, exc):
        return JSONResponse(status_code=exc.http_status,
                            content={"code": exc.code, "message": exc.message, "data": exc.data})

    with TestClient(app) as client:
        yield client, user


def test_api_overview_preview_save_candidates_and_audit(mem_db, scope_api):
    api, _user = scope_api
    tenant(mem_db, 11)
    tenant(mem_db, 22)
    for tid, rid, name in ((11, "i-1", "redis-prod-1"), (11, "i-2", "redis-cache-1"), (11, "i-3", "redis-cache-2"),
                           (22, "x-1", "redis-cache-x")):
        add_resource(mem_db, "dcs_instance", rid, name, tid=tid)
    mem_db.commit()
    base = "/api/v1/system/collect-scopes"

    data = api.get(base).json()["data"]
    types = {t["code"]: t for t in data["types"]}
    assert len(types) == 15 and data["global_editable"] is True
    assert [d["key"] for d in types["cce_workload"]["dimensions"]] == ["cluster", "namespace", "workload"]
    assert [d["key"] for d in types["dgc_batch_job"]["dimensions"]] == ["workspace", "job"]
    assert [d["key"] for d in types["dcs_instance"]["dimensions"]] == ["name", "id", "env", "business", "owner",
                                                                        "critical"]
    assert types["dcs_instance"]["tenants"]["11"] == {
        "setting": None, "source": "none", "rules": [], "resources": 3, "excluded": 0, "updated_by": None,
        "updated_at": None}

    body = {"rules": [{"effect": "exclude", "match": {"name": ["redis-cache-*"]}, "note": "缓存实例由其他团队负责"}]}
    preview = api.post(f"{base}/11/dcs_instance/preview", json=body).json()["data"]
    assert (preview["total"], preview["excluded"], preview["newly_excluded"]) == (3, 2, 2)
    assert flags(mem_db, "dcs_instance")["redis-cache-1"] is False                    # 预览不落库

    saved = api.put(f"{base}/11/dcs_instance", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["excluded"] == 2
    assert flags(mem_db, "dcs_instance") == {"redis-prod-1": False, "redis-cache-1": True, "redis-cache-2": True,
                                             "redis-cache-x": False}                 # 其他租户不受影响
    shown = api.get(base).json()["data"]["types"]
    row = next(t for t in shown if t["code"] == "dcs_instance")["tenants"]["11"]
    assert row["source"] == "tenant" and row["excluded"] == 2 and row["updated_by"] == "Tester"
    assert row["setting"][0]["note"] == "缓存实例由其他团队负责"

    names = api.get(f"{base}/11/dcs_instance/candidates", params={"dim": "name", "keyword": "cache"}).json()["data"]
    assert [c["value"] for c in names] == ["redis-cache-1", "redis-cache-2"]

    audit = mem_db.scalar(select(AuditLog).where(AuditLog.action == "collect.scope"))
    assert audit.tenant_id == 11 and audit.detail["before"] == ["跟随全局"]
    assert audit.detail["after"] == ["排除：名称 = redis-cache-*"] and audit.detail["excluded"] == 2

    assert api.put(f"{base}/11/dcs_instance", json=body).json()["data"]["changed"] is False
    count = mem_db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "collect.scope"))
    assert count == 1                                                                 # 没有变化不写审计

    restored = api.put(f"{base}/11/dcs_instance", json={"rules": None}).json()["data"]
    assert restored["restored"] == 2 and flags(mem_db, "dcs_instance")["redis-cache-1"] is False


def test_api_global_scope_applies_to_tenants_without_their_own_setting(mem_db, scope_api):
    api, _user = scope_api
    tenant(mem_db, 11)
    tenant(mem_db, 22)
    add_resource(mem_db, "dcs_instance", "i-1", "redis-cache-1", tid=11)
    add_resource(mem_db, "dcs_instance", "x-1", "redis-cache-x", tid=22)
    mem_db.commit()
    base = "/api/v1/system/collect-scopes"
    body = {"rules": [{"effect": "exclude", "match": {"name": ["redis-cache-*"]}}]}
    assert api.put(f"{base}/22/dcs_instance", json={"rules": []}).status_code == 200      # 租户 22 明确不限制
    assert api.put(f"{base}/0/dcs_instance", json=body).json()["data"]["excluded"] == 1
    assert flags(mem_db, "dcs_instance") == {"redis-cache-1": True, "redis-cache-x": False}
    assert api.put(f"{base}/0/dcs_instance", json={"rules": None}).status_code == 400   # 全局不能“跟随全局”
    assert api.put(f"{base}/0/dcs_instance", json={"rules": []}).json()["data"]["restored"] == 1


def test_api_validation_permissions_and_tenant_isolation(mem_db, scope_api):
    api, user = scope_api
    tenant(mem_db, 11)
    tenant(mem_db, 22)
    base = "/api/v1/system/collect-scopes"
    ok_body = {"rules": [{"effect": "exclude", "match": {"name": ["x"]}}]}
    assert api.put(f"{base}/11/nope", json=ok_body).status_code == 400
    assert api.put(f"{base}/99/dcs_instance", json=ok_body).status_code == 400
    assert api.put(f"{base}/11/cce_workload",
                   json={"rules": [{"effect": "exclude", "match": {"business": ["x"]}}]}).status_code == 400
    assert api.put(f"{base}/11/dcs_instance",
                   json={"rules": [{"effect": "exclude", "match": {"name": []}}]}).status_code in (400, 422)
    assert api.put(f"{base}/11/dcs_instance", json={"rules": [{"effect": "maybe", "match": {"name": ["x"]}}]}
                   ).status_code in (400, 422)
    assert api.put(f"{base}/11/dcs_instance", json={}).status_code in (400, 422)           # rules 必须显式给出
    assert api.get(f"{base}/11/dcs_instance/candidates", params={"dim": "business"}).status_code == 200
    assert api.get(f"{base}/11/cce_workload/candidates", params={"dim": "business"}).status_code == 400

    user.permissions = ["task:view"]                                                      # 只读
    assert api.get(base).status_code == 200
    assert api.put(f"{base}/11/dcs_instance", json=ok_body).status_code == 403
    assert api.post(f"{base}/11/dcs_instance/preview", json=ok_body).status_code == 403
    user.permissions = ["collect:manage"]
    assert api.get(base).status_code == 200
    user.permissions = ["collect:manage", "task:view"]
    user.tenant_ids = [11]                                                                # 部分租户用户
    assert api.put(f"{base}/11/dcs_instance", json=ok_body).status_code == 200
    assert api.put(f"{base}/22/dcs_instance", json=ok_body).status_code == 403
    assert api.put(f"{base}/0/dcs_instance", json=ok_body).status_code == 403
    assert api.get(f"{base}/0/dcs_instance/candidates", params={"dim": "name"}).status_code == 403
    visible = api.get(base).json()["data"]
    assert [t["code"] for t in visible["tenants"]] == ["tenant-11"] and visible["global_editable"] is False


def test_collection_policy_selection_and_scope_are_independent_settings(mem_db):
    """ECS 的 selection（旧策略）与采集范围叠加：任一方排除即不采集，互不覆盖。"""
    tenant(mem_db)
    first = add_resource(mem_db, "ecs_server", "e-1", "web-1", critical=True)
    add_resource(mem_db, "ecs_server", "e-2", "web-2", critical=True)
    add_resource(mem_db, "ecs_server", "e-3", "db-1", critical=False)
    mem_db.commit()
    critical_only = CollectionPolicy(selection={"mode": "critical"})
    assert {r.name for r in collection_policy.select_resources(mem_db, 11, 101, critical_only)} == {"web-1", "web-2"}
    collect_scope.save(mem_db, 11, "ecs_server", [exclude(name="web-2")], "admin")
    mem_db.commit()
    assert [r.id for r in collection_policy.select_resources(mem_db, 11, 101, critical_only)] == [first.id]
    assert flags(mem_db, "ecs_server") == {"web-1": False, "web-2": True, "db-1": False}
    assert mem_db.scalar(select(SysSetting).where(SysSetting.key == collection_policy.policy_key(11))) is None


def test_scope_settings_are_plain_sys_setting_rows_with_bounded_keys(mem_db):
    tenant(mem_db)
    longest = max(len(collect_scope.setting_key(999999, code)) for code in RESOURCE_TYPES)
    assert longest <= 64                                                                  # sys_setting.key 为 VARCHAR(64)
    collect_scope.save(mem_db, 11, "mrs_job", [include(workspace="ws-1")], "admin")
    mem_db.commit()
    row = mem_db.get(SysSetting, "collect_scope:11:mrs_job")
    assert row.value == {"rules": [include(workspace="ws-1")]} and row.updated_by == "admin"
