"""DCS 分片与节点：§4.3.3 批量查询实例节点信息（GET /v2/{project_id}/instances-logical-nodes）。

节点状态枚举见表 4-109：Creating / Active / Inactive / Deleting / AddSharding；node_role 为 redis-server / redis-proxy，
node_type 为 master / slave / proxy。只支持 Redis 4.0 及以上，创建中实例不返回节点。全部为合成数据。
"""
from datetime import UTC, datetime, timedelta

import pytest

from app.collectors import dcs
from app.collectors.base import RunContext
from app.collectors.dcs import DcsCollector, node_record, node_summary
from app.hcs import HcsError

AT = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)
NODES = "/v2/{project_id}/instances-logical-nodes"


def raw_node(ip, role="slave", status="Active", group=0, kind=None, name=None):
    kind = kind or ("redis-proxy" if role == "proxy" else "redis-server")
    server = kind == "redis-server"
    return {"logical_node_id": f"l-{ip}", "name": name or f"({role}){ip}", "status": status, "az_code": "az1",
            "node_role": kind, "node_type": role, "node_ip": ip, "node_port": "6379", "node_id": f"n-{ip}",
            "group_id": f"g-{group}" if server else "", "group_name": f"group-{group}" if server else ""}


def ha(master="Active", slave="Active"):
    return [raw_node("10.0.0.1", "master", master), raw_node("10.0.0.2", "slave", slave)]


class Client:
    def __init__(self, nodes=None, instances=None, *, error=None, used_cpu="130"):
        self.calls = []
        self.nodes = {"one": ha()} if nodes is None else nodes
        self.instances = instances or [{"instance_id": "one", "name": "redis", "status": "RUNNING", "engine": "Redis",
                                        "engine_version": "5.0", "used_memory": 10, "max_memory": 100}]
        self.error, self.used_cpu = error, used_cpu

    def get(self, service, path, *, params=None):
        self.calls.append((path, params))
        if path == NODES:
            if self.error:
                raise self.error
            rows = [{"instance_id": iid, "node_count": len(nodes), "nodes": nodes} for iid, nodes in self.nodes.items()]
            return {"count": len(rows), "instances": rows}
        if path.endswith("/statistic"):
            return {"statistics": [{"instance_id": i["instance_id"], "used_memory": 10, "max_memory": 100,
                                    "used_cpu": self.used_cpu} for i in self.instances if i["status"] == "RUNNING"]}
        return {"instance_num": len(self.instances), "instances": self.instances}


def collect(monkeypatch, client, previous_cpu=None, previous_topology=None):
    monkeypatch.setattr(dcs, "utcnow", lambda: AT)
    monkeypatch.setattr(dcs, "previous_cpu_counters", lambda _ctx, _ids: previous_cpu or {})
    monkeypatch.setattr(dcs, "previous_attrs", lambda _ctx, _type, _ids, _key: previous_topology or {})
    ctx = RunContext(client=client, account="a", project_id="p", tenant_id=1, project_ref=2)
    return DcsCollector().collect(ctx), ctx


def by_check(rows, iid="one"):
    return {row.check_item: row for row in rows if row.resource_id == iid}


def test_one_batch_request_per_round_with_the_documented_page_size(monkeypatch):
    client = Client()
    collect(monkeypatch, client)
    assert [call for call in client.calls if call[0] == NODES] == [(NODES, {"offset": 0, "limit": 100})]


def test_inactive_replica_is_reported_on_the_instance_with_shard_role_and_address(monkeypatch):
    rows, ctx = collect(monkeypatch, Client({"one": ha(slave="Inactive")}))
    checks = by_check(rows)
    assert checks["nodes"].facts == {"instance_status": "RUNNING", "inactive_masters": 0, "inactive_other_nodes": 1,
                                     "unknown_nodes": 0, "inactive_list": "group-0 从 10.0.0.2:6379"}
    assert checks["nodes"].sub_object == "" and checks["nodes"].metric_observed_at == AT
    attrs = checks["instance_status"].attrs
    assert [n["name"] for n in attrs["nodes"]] == ["10.0.0.1:6379", "10.0.0.2:6379"]
    assert attrs["nodes"][1] == {"name": "10.0.0.2:6379", "node_name": "(slave)10.0.0.2", "role": "slave",
                                 "kind": "redis-server", "status": "Inactive", "shard": "group-0", "shard_id": "g-0",
                                 "ip": "10.0.0.2", "port": "6379", "az": "az1"}
    topology = attrs["dcs_topology"]
    assert topology["state"] == "AVAILABLE" and topology["observed_at"] == AT.isoformat()
    assert {k: topology[k] for k in ("node_count", "shards", "masters", "replicas", "proxies", "inactive",
                                     "inactive_masters", "unknown")} == {
        "node_count": 2, "shards": 1, "masters": 1, "replicas": 1, "proxies": 0, "inactive": 1,
        "inactive_masters": 0, "unknown": 0}
    assert ctx.errors == [] and ctx.stats["logical_nodes"] == 2


def test_proxy_cluster_counts_shards_masters_and_proxies_and_lists_masters_first(monkeypatch):
    nodes = [raw_node("10.0.1.1", "master", group=0), raw_node("10.0.1.2", "slave", "Inactive", group=0),
             raw_node("10.0.1.3", "master", "Inactive", group=1), raw_node("10.0.1.4", "slave", group=1),
             raw_node("10.0.1.9", "proxy", "Inactive")]
    rows, _ = collect(monkeypatch, Client({"one": nodes}))
    facts = by_check(rows)["nodes"].facts
    assert (facts["inactive_masters"], facts["inactive_other_nodes"]) == (1, 2)
    assert facts["inactive_list"] == "group-1 主 10.0.1.3:6379、Proxy 10.0.1.9:6379、group-0 从 10.0.1.2:6379"
    topology = by_check(rows)["instance_status"].attrs["dcs_topology"]
    assert (topology["shards"], topology["masters"], topology["replicas"], topology["proxies"]) == (2, 2, 2, 1)


def test_long_failure_lists_are_truncated_with_the_total():
    nodes = [node_record(raw_node(f"10.0.2.{i}", "slave", "Inactive", group=i)) for i in range(7)]
    listed = node_summary(nodes)["inactive_list"]
    assert listed.endswith(" 等 7 个") and listed.count("、") == 4


def test_master_count_for_cpu_comes_from_the_nodes_not_the_list_api(monkeypatch):
    # §4.1.2 实例列表没有 cache_mode / sharding_count；3 个分片 90 秒 CPU / 60 秒 → 每个主节点 50%
    nodes = [raw_node(f"10.0.3.{g}", "master", group=g) for g in range(3)]
    previous = {"one": {"seconds": 100, "at": (AT - timedelta(seconds=60)).isoformat()}}
    rows, _ = collect(monkeypatch, Client({"one": nodes}, used_cpu="190"), previous_cpu=previous)
    assert by_check(rows)["cpu"].facts == {"cpu_usage": 50.0}


@pytest.mark.parametrize("engine,version", [("Memcached", "1.5"), ("Redis", "3.0")])
def test_engines_without_logical_nodes_are_not_applicable_rather_than_missing(monkeypatch, engine, version):
    instance = {"instance_id": "one", "name": "cache", "status": "RUNNING", "engine": engine, "engine_version": version,
                "used_memory": 10, "max_memory": 100}
    rows, ctx = collect(monkeypatch, Client({}, [instance]))
    checks = by_check(rows)
    assert checks["nodes"].facts == {"instance_status": "RUNNING", "inactive_masters": 0, "inactive_other_nodes": 0,
                                     "unknown_nodes": 0, "inactive_list": ""}
    assert checks["instance_status"].attrs["dcs_topology"]["state"] == "NOT_SUPPORTED"
    assert checks["instance_status"].attrs["nodes"] == [] and ctx.errors == []


def test_instance_missing_from_the_response_is_not_evidence_of_health(monkeypatch):
    rows, ctx = collect(monkeypatch, Client({}))
    checks = by_check(rows)
    assert "nodes" not in checks                                           # 没有观测：按缺采处理，不恢复告警
    assert checks["instance_status"].attrs["dcs_topology"]["state"] == "NOT_RETURNED"
    assert checks["instance_status"].attrs["nodes"] == [] and ctx.errors == []


def test_api_failure_keeps_the_last_nodes_marks_them_stale_and_keeps_cpu_normalised(monkeypatch):
    previous_topology = {"one": {"state": "AVAILABLE", "observed_at": (AT - timedelta(minutes=5)).isoformat(),
                                 "node_count": 6, "shards": 3, "masters": 3, "inactive": 0, "warning": None}}
    previous_cpu = {"one": {"seconds": 100, "at": (AT - timedelta(seconds=60)).isoformat()}}
    client = Client(error=HcsError(503, "DCS.5000", "synthetic"), used_cpu="190")
    rows, ctx = collect(monkeypatch, client, previous_cpu=previous_cpu, previous_topology=previous_topology)
    checks = by_check(rows)
    assert "nodes" not in checks and "nodes" not in checks["instance_status"].attrs     # 不改写上次的节点清单
    topology = checks["instance_status"].attrs["dcs_topology"]
    assert topology["state"] == "STALE" and topology["masters"] == 3
    assert topology["observed_at"] == previous_topology["one"]["observed_at"]
    assert checks["cpu"].facts == {"cpu_usage": 50.0}                                  # 主节点数沿用上次结果
    assert ctx.errors and "logical_nodes" not in ctx.stats


def test_first_failure_without_history_is_unavailable(monkeypatch):
    rows, _ = collect(monkeypatch, Client(error=HcsError(0, "NETWORK", "synthetic")))
    assert by_check(rows)["instance_status"].attrs["dcs_topology"] == {
        "state": "UNAVAILABLE", "observed_at": None, "warning": "节点接口调用失败"}


@pytest.mark.parametrize("body", [{"count": 1, "instances": [{"instance_id": "one", "nodes": "bad"}]},
                                  {"count": 2, "instances": [{"instance_id": "one", "nodes": []},
                                                             {"instance_id": "one", "nodes": []}]},
                                  {"count": 1, "instances": [{"nodes": []}]}, {"instances": None}])
def test_malformed_responses_discard_the_whole_batch(monkeypatch, body):
    client = Client()
    client.get = lambda service, path, *, params=None: body if path == NODES else Client.get(Client(), service, path,
                                                                                            params=params)
    rows, ctx = collect(monkeypatch, client)
    assert "nodes" not in by_check(rows) and ctx.errors


def test_unknown_node_status_is_counted_and_role_is_inferred_from_node_type(monkeypatch):
    nodes = [raw_node("10.0.4.1", "master"), {**raw_node("10.0.4.2", "slave", "Rebuilding"), "node_role": None}]
    rows, _ = collect(monkeypatch, Client({"one": nodes}))
    assert by_check(rows)["nodes"].facts["unknown_nodes"] == 1
    assert by_check(rows)["instance_status"].attrs["nodes"][1]["kind"] == "redis-server"


def test_creating_instances_do_not_trigger_a_node_request(monkeypatch):
    instance = {"instance_id": "one", "name": "new", "status": "CREATING", "engine": "Redis", "engine_version": "6.0"}
    client = Client({}, [instance])
    rows, _ = collect(monkeypatch, client)
    assert [path for path, _ in client.calls] == ["/v2/{project_id}/instances"]
    assert by_check(rows)["instance_status"].attrs["dcs_topology"]["state"] == "NOT_QUERIED"


def rule(code):
    from app.rules.loader import load_yaml_rules
    from app.rules.model import Rule

    raw = next(r for r in load_yaml_rules() if r["id"] == code)
    return Rule(**{"scope": {}, "recover": {}, "for_count": 1, **raw})


FACTS = {"instance_status": "RUNNING", "inactive_masters": 0, "inactive_other_nodes": 0, "unknown_nodes": 0,
         "inactive_list": "", "critical": False, "expected_running": True}


def test_master_rule_fires_p1_only_while_the_instance_runs_and_recovers_at_zero():
    master = rule("R-DCS-008")
    assert master.check_item == "nodes" and master.for_count == 2
    hit = master.match({**FACTS, "inactive_masters": 1, "inactive_list": "group-0 主 10.0.0.1:6379"})
    assert hit.severity == "P1" and hit.title == "1 个主节点故障：group-0 主 10.0.0.1:6379"
    assert master.match({**FACTS, "instance_status": "RESTARTING", "inactive_masters": 1}) is None
    assert master.recovered({**FACTS, "instance_status": "RESTARTING", "inactive_masters": 1}) is False
    assert master.recovered(FACTS) is True


def test_replica_rule_is_p2_critical_p1_and_ignores_master_failures():
    replica = rule("R-DCS-009")
    assert replica.match({**FACTS, "inactive_other_nodes": 2}).severity == "P2"
    assert replica.match({**FACTS, "inactive_other_nodes": 2, "critical": True}).severity == "P1"
    assert replica.match({**FACTS, "inactive_masters": 1}) is None
    assert replica.recovered({**FACTS, "inactive_masters": 1}) is True


def test_unknown_node_states_never_count_as_recovered():
    from app.collectors.base import Observation
    from app.services.inspection_evidence import state_known

    known = Observation("dcs_instance", "one", "redis", "nodes", dict(FACTS))
    unknown = Observation("dcs_instance", "one", "redis", "nodes", {**FACTS, "unknown_nodes": 1})
    assert state_known(known) is True and state_known(unknown) is False


def test_node_probe_uses_the_documented_page_size_and_instance_count():
    from types import SimpleNamespace

    from app.tools.probe import PROBES, Runner

    descriptor = next(item for item in PROBES if item.code == "dcs.nodes")
    calls = []

    def get(service, path, **kwargs):
        calls.append((service, path, kwargs))
        return {"count": 1, "instances": [{"instance_id": "one", "node_count": 2, "nodes": ha()}]}

    assert Runner(SimpleNamespace(get=get), SimpleNamespace(), 500, False).run(descriptor)["result"] == "OK"
    assert calls[0][:2] == ("dcs", NODES) and calls[0][2]["params"]["limit"] == 100


def test_mock_pipeline_alarms_on_an_inactive_replica_and_recovers_when_it_is_back(mem_db, monkeypatch):
    from sqlalchemy import select

    from app.models import AlarmEvent, Resource
    from app.services.pipeline import run_task
    from mock_hcs import inventory
    from tests.test_pipeline import _client, _setup

    _setup(mem_db)
    cache = inventory.find("dcs", "redis-cache-02")                       # Mock 中为 3 分片集群
    monkeypatch.setitem(cache, "node_status", {"group-1/slave": "Inactive"})
    client = _client()
    for _ in range(2):                                                    # R-DCS-009 连续 2 次命中才告警
        run = run_task(mem_db, "dcs_status", client=client)
        assert run.status == "SUCCESS", run.error_summary
    alarm = mem_db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-DCS-009"))
    assert (alarm.resource_name, alarm.severity, alarm.status) == ("redis-cache-02", "P2", "FIRING")
    assert "group-1 从" in alarm.title and alarm.sub_object == ""
    assert mem_db.scalar(select(AlarmEvent).where(AlarmEvent.rule_id == "R-DCS-008")) is None
    resource = mem_db.scalar(select(Resource).where(Resource.name == "redis-cache-02"))
    assert resource.attrs["dcs_topology"]["shards"] == 3 and resource.attrs["dcs_topology"]["inactive"] == 1
    assert len(resource.attrs["nodes"]) == 6
    monkeypatch.setitem(cache, "node_status", {})
    assert run_task(mem_db, "dcs_status", client=client).status == "SUCCESS"
    mem_db.refresh(alarm)
    assert alarm.status == "RESOLVED"
