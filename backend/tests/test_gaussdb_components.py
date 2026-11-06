"""GaussDB 组件状态：§4.3.10 查询实例的组件列表（表 4-80 组件状态 Primary / Normal / Down / Standby / StateFollower /
StateLeader / StateCandidate）。在线实例（ACTIVE、STORAGE FULL、BACKING UP）每轮查询磁盘用量与组件；全部为合成数据。"""
from datetime import UTC, datetime

import pytest

from app.collectors import gaussdb
from app.collectors.base import RunContext
from app.collectors.gaussdb import GaussDBCollector
from app.hcs import HcsError

AT = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)
LIST = "/gaussdb/v3.3/{project_id}/instances"


def component(cid, kind="DN", status="Primary", role="master", node="node-1"):
    return {"node": node, "item": {"id": cid, "type": kind, "role": role, "status": status, "distributed_id": "g-1"}}


class Cloud:
    def __init__(self, status="ACTIVE", items=None, error=None):
        self.status, self.error, self.calls = status, error, []
        self.items = [component("dn_6001"), component("dn_6002", status="Standby", role="slave", node="node-2")] \
            if items is None else items

    def get(self, service, path, *, params=None):
        self.calls.append(path.rsplit("/", 1)[-1])
        if path == LIST:
            return {"total_count": 1, "instances": [{"id": "g1", "name": "gs-core", "status": self.status, "nodes": []}]}
        if path.endswith("/volume-usage"):
            return {"used": "50", "total": "200"}
        assert path.endswith("/components")
        if self.error:
            raise self.error
        nodes: dict[str, dict] = {}
        for entry in self.items:
            nodes.setdefault(entry["node"], {"id": entry["node"], "name": f"name-{entry['node']}", "components": []})[
                "components"].append(entry["item"])
        return {"total_count": len(nodes), "nodes": list(nodes.values())}


def collect(monkeypatch, cloud, previous=None):
    monkeypatch.setattr(gaussdb, "utcnow", lambda: AT)
    monkeypatch.setattr(gaussdb, "previous_attrs", lambda _ctx, _type, _ids, _key: previous or {})
    ctx = RunContext(client=cloud, account="a", project_id="p", tenant_id=1, project_ref=2)
    rows = GaussDBCollector().collect(ctx)
    return {row.check_item: row for row in rows}, ctx


@pytest.mark.parametrize("status", ["ACTIVE", "STORAGE FULL", "BACKING UP"])
def test_online_instances_query_disk_and_components(monkeypatch, status):
    cloud = Cloud(status)
    rows, ctx = collect(monkeypatch, cloud)
    assert cloud.calls == ["instances", "volume-usage", "components"]
    assert rows["disk"].facts == {"disk_usage": 25.0}                    # 磁盘满时正需要用量
    assert rows["components"].facts == {"down_components": 0, "unknown_components": 0, "down_list": ""}
    summary = rows["instance_status"].attrs["gaussdb_components"]
    assert (summary["state"], summary["total"], summary["by_type"], summary["down"]) == ("AVAILABLE", 2, {"DN": 2}, 0)
    assert summary["observed_at"] == AT.isoformat() and ctx.errors == []
    assert ctx.stats["native_components"] == 1


@pytest.mark.parametrize("status", ["BUILD", "REBOOTING", "FAILED", "SHUTDOWN", "FROZEN"])
def test_offline_or_changing_instances_are_not_queried(monkeypatch, status):
    cloud = Cloud(status)
    rows, _ = collect(monkeypatch, cloud)
    assert cloud.calls == ["instances"] and "components" not in rows
    assert "gaussdb_components" not in rows["instance_status"].attrs     # 不改写上次结果


def test_down_components_are_counted_and_listed_with_their_node(monkeypatch):
    items = [component("cn_5001", "CN", "Normal", None, "node-1"), component("dn_6002", "DN", "Down", "slave", "node-2"),
             component("etcd_1", "ETCD", "StateLeader", None, "node-3"), component("gtm_1", "GTM", "Down", None, "node-3")]
    rows, _ = collect(monkeypatch, Cloud(items=items))
    assert rows["components"].facts == {"down_components": 2, "unknown_components": 0,
                                        "down_list": "DN dn_6002（name-node-2）、GTM gtm_1（name-node-3）"}
    assert rows["instance_status"].attrs["gaussdb_components"]["by_type"] == {"CN": 1, "DN": 1, "ETCD": 1, "GTM": 1}


def test_undocumented_component_status_is_counted_as_unknown(monkeypatch):
    rows, _ = collect(monkeypatch, Cloud(items=[component("dn_6001", status="ACTIVE")]))
    assert rows["components"].facts["unknown_components"] == 1
    assert rows["instance_status"].attrs["gaussdb_components"]["warning"] == "部分组件状态未识别，组件告警暂不恢复"


def test_empty_component_list_is_not_evidence_of_health(monkeypatch):
    rows, ctx = collect(monkeypatch, Cloud(items=[]))
    assert "components" not in rows and ctx.errors == []
    assert rows["instance_status"].attrs["gaussdb_components"]["state"] == "NO_DATA"


def test_failure_keeps_the_previous_components_marked_stale_and_disk_still_collected(monkeypatch):
    previous = {"g1": {"state": "AVAILABLE", "observed_at": "2026-11-06T07:55:00+00:00", "total": 3, "down": 0,
                       "items": [{"component_id": "dn_6001"}]}}
    rows, ctx = collect(monkeypatch, Cloud(error=HcsError(503, "DBS.5000", "synthetic")), previous)
    assert "components" not in rows and rows["disk"].facts == {"disk_usage": 25.0}
    summary = rows["instance_status"].attrs["gaussdb_components"]
    assert summary["state"] == "STALE" and summary["observed_at"] == previous["g1"]["observed_at"]
    assert summary["items"] == previous["g1"]["items"]
    assert ctx.errors == ["GaussDB 组件状态查询失败（HTTP 503 / DBS.5000）"]


def test_first_failure_without_history_is_an_error_state(monkeypatch):
    rows, _ = collect(monkeypatch, Cloud(error=HcsError(0, "NETWORK", "synthetic")))
    assert rows["instance_status"].attrs["gaussdb_components"] == {
        "state": "ERROR", "observed_at": None, "warning": "组件查询失败"}


def test_component_rule_is_p2_critical_p1_and_recovers_only_at_zero():
    from app.rules.loader import load_yaml_rules
    from app.rules.model import Rule

    raw = next(r for r in load_yaml_rules() if r["id"] == "R-GS-012")
    rule = Rule(**{"scope": {}, "recover": {}, "for_count": 1, **raw})
    facts = {"down_components": 1, "unknown_components": 0, "down_list": "DN dn_6002（node-2）", "critical": False}
    assert rule.check_item == "components" and rule.for_count == 2
    hit = rule.match(facts)
    assert hit.severity == "P2" and hit.title == "1 个组件宕机：DN dn_6002（node-2）"
    assert rule.match({**facts, "critical": True}).severity == "P1"
    assert rule.recovered({**facts, "down_components": 0}) is True and rule.recovered(facts) is False


def test_unknown_component_states_never_count_as_recovered():
    from app.collectors.base import Observation
    from app.services.inspection_evidence import state_known

    facts = {"down_components": 0, "unknown_components": 0, "down_list": ""}
    assert state_known(Observation("gaussdb_instance", "g1", "gs", "components", facts)) is True
    assert state_known(Observation("gaussdb_instance", "g1", "gs", "components", {**facts, "unknown_components": 2})) is False
