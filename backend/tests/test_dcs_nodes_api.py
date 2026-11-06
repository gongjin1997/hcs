"""DCS 实例接口里的分片与节点：列表只带汇总，详情带节点清单；都读本地记录，不请求云。"""
from datetime import UTC, datetime, timedelta

from app.collectors.dcs import node_record, node_summary
from tests.test_dcs_history_api import dcs_api as dcs_api
from tests.test_dcs_history_api import resource
from tests.test_dcs_nodes import ha


def topology(nodes, observed):
    summary = {k: v for k, v in node_summary(nodes).items() if k != "inactive_list"}
    return {"state": "AVAILABLE", "observed_at": observed.isoformat(), "warning": "有节点故障", **summary}


def test_lists_return_the_topology_summary_and_only_the_detail_returns_nodes(mem_db, dcs_api):
    client, _user, _app, calls = dcs_api
    nodes = [node_record(n) for n in ha(slave="Inactive")]
    row = resource(mem_db, attrs={"engine": "Redis", "nodes": nodes, "dcs_topology": topology(nodes, datetime.now(UTC))})
    listed = client.get("/api/v1/dcs/instances").json()["data"]["items"][0]
    assert listed["nodes"] is None
    assert listed["topology"]["state"] == "AVAILABLE" and listed["topology"]["freshness"] == "FRESH"
    assert (listed["topology"]["masters"], listed["topology"]["inactive"]) == (1, 1)
    detail = client.get(f"/api/v1/dcs/instances/{row.id}").json()["data"]
    assert [node["name"] for node in detail["nodes"]] == ["10.0.0.1:6379", "10.0.0.2:6379"]
    assert detail["nodes"][1]["status"] == "Inactive" and detail["nodes"][1]["role"] == "slave"
    assert not calls


def test_failed_or_old_topology_is_never_shown_as_fresh(mem_db, dcs_api):
    client = dcs_api[0]
    now = datetime.now(UTC)
    resource(mem_db, name="old", attrs={"dcs_topology": {"state": "AVAILABLE", "masters": 1,
                                                         "observed_at": (now - timedelta(hours=2)).isoformat()}})
    resource(mem_db, name="stale", attrs={"dcs_topology": {"state": "STALE", "observed_at": now.isoformat()}})
    resource(mem_db, name="never", attrs={"dcs_topology": {"state": "bogus"}})
    items = {item["name"]: item for item in client.get("/api/v1/dcs/instances").json()["data"]["items"]}
    assert items["old"]["topology"]["freshness"] == "STALE"
    assert items["stale"]["topology"]["freshness"] == "STALE"
    assert items["never"]["topology"] is None
