"""DCS CPU：v2 instances/statistic 的 used_cpu 是累计 CPU 秒数（API §4.2.6），不是百分比。
由两次采样折算“每个主节点占用的 CPU”；折算不了就不输出，不补零、不沿用旧值。"""
from datetime import UTC, datetime, timedelta

import pytest

from app.collectors import dcs
from app.collectors.base import RunContext
from app.collectors.dcs import DcsCollector, cpu_rate, master_count

AT = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)


def counter(seconds, ago):
    return {"seconds": seconds, "at": (AT - timedelta(seconds=ago)).isoformat()}


@pytest.mark.parametrize("previous,seconds,masters,expected", [
    (counter(100, 60), 130.0, 1, 50.0),                 # 60 秒用了 30 秒 CPU → 半个核
    (counter(100, 60), 190.0, 3, 50.0),                 # 3 个分片共用了 90 秒 CPU → 每个主节点 50%
    (counter(100, 60), 100.0, 1, 0.0),                  # 真实的 0 保持 0
    (counter(100, 120), 112.0, 2, 5.0),
    (counter(100, 60), 160.0, 1, 100.0),                # 跑满一个核
    (counter(100, 60), 99.0, 1, None),                  # 计数器回退：实例重启
    (counter(100, 10), 130.0, 1, None),                 # 间隔太短
    (counter(100, 3600), 130.0, 1, None),               # 间隔太长：期间采集中断过
    (counter(100, 60), 130.0, None, None),              # 拿不到主节点数
    (counter(100, 60), None, 1, None),                  # 本次没有累计值
    (None, 130.0, 1, None),                             # 首次采样
    ({"seconds": True, "at": counter(0, 60)["at"]}, 130.0, 1, None),
    ({"seconds": 100, "at": "not-a-time"}, 130.0, 1, None),
    ({"seconds": 100}, 130.0, 1, None),
    ("garbage", 130.0, 1, None),
])
def test_cpu_rate_from_two_cumulative_samples(previous, seconds, masters, expected):
    assert cpu_rate(previous, seconds, AT, masters) == expected


def test_cpu_rate_needs_the_sample_time():
    assert cpu_rate(counter(100, 60), 130.0, None, 1) is None


@pytest.mark.parametrize("instance,expected", [
    ({"cache_mode": "single"}, 1), ({"cache_mode": "ha"}, 1), ({"cache_mode": "ha_rw_split"}, 1),
    ({"cache_mode": "cluster", "sharding_count": 4}, 4), ({"cache_mode": "proxy", "sharding_count": 8}, 8),
    ({"cache_mode": "cluster"}, None), ({"cache_mode": "cluster", "sharding_count": 0}, None),
    ({"cache_mode": "cluster", "sharding_count": "4"}, None), ({"cache_mode": "cluster", "sharding_count": True}, None),
    ({}, None), ({"cache_mode": "unknown", "sharding_count": 2}, None)])
def test_master_count_follows_the_documented_cache_modes(instance, expected):
    assert master_count(instance) == expected


class Client:
    def __init__(self, used_cpu, **instance):
        self.used_cpu, self.instance = used_cpu, instance

    def get(self, service, path, **kwargs):
        if path.endswith("/statistic"):
            return {"statistics": [{"instance_id": "one", "used_memory": 10, "max_memory": 100,
                                    "used_cpu": self.used_cpu}]}
        return {"instance_num": 1, "instances": [{"instance_id": "one", "name": "redis", "status": "RUNNING",
                                                  "used_memory": 10, "max_memory": 100, **self.instance}]}


def collect(monkeypatch, previous, used_cpu="130", **instance):
    monkeypatch.setattr(dcs, "utcnow", lambda: AT)
    monkeypatch.setattr(dcs, "previous_cpu_counters", lambda _ctx, _ids: previous)
    ctx = RunContext(client=Client(used_cpu, **instance), account="a", project_id="p", tenant_id=1, project_ref=2)
    return DcsCollector().collect(ctx)


def test_collector_emits_a_cpu_observation_only_when_it_can_be_derived(monkeypatch):
    rows = collect(monkeypatch, {"one": counter(100, 60)}, cache_mode="ha")
    by_check = {r.check_item: r for r in rows}
    assert list(by_check) == ["instance_status", "memory", "cpu"]
    assert by_check["cpu"].facts == {"cpu_usage": 50.0} and by_check["cpu"].metrics == {"cpu_usage": 50.0}
    assert by_check["instance_status"].metrics["cpu_usage"] == 50.0
    assert by_check["instance_status"].metrics["used_cpu_seconds"] == 130.0
    assert by_check["instance_status"].attrs["dcs_cpu_counter"] == {"seconds": 130.0, "at": AT.isoformat()}


def test_first_sample_stores_the_baseline_but_reports_no_cpu(monkeypatch):
    rows = collect(monkeypatch, {}, cache_mode="ha")
    assert [r.check_item for r in rows] == ["instance_status", "memory"]
    status = rows[0]
    assert status.metrics["cpu_usage"] is None
    assert status.attrs["dcs_cpu_counter"] == {"seconds": 130.0, "at": AT.isoformat()}     # 下一轮据此折算


def test_cluster_without_a_shard_count_is_not_normalised_by_guessing(monkeypatch):
    rows = collect(monkeypatch, {"one": counter(100, 60)}, cache_mode="cluster")
    assert [r.check_item for r in rows] == ["instance_status", "memory"]
    rows = collect(monkeypatch, {"one": counter(100, 60)}, used_cpu="190", cache_mode="cluster", sharding_count=3)
    assert next(r for r in rows if r.check_item == "cpu").facts == {"cpu_usage": 50.0}


def test_restart_resets_the_baseline_without_a_negative_or_zero_cpu(monkeypatch):
    rows = collect(monkeypatch, {"one": counter(5000, 60)}, used_cpu="3", cache_mode="ha")
    assert [r.check_item for r in rows] == ["instance_status", "memory"]
    assert rows[0].attrs["dcs_cpu_counter"]["seconds"] == 3.0                              # 新基线是重启后的值


def test_missing_counter_keeps_the_previous_baseline_untouched(monkeypatch):
    rows = collect(monkeypatch, {"one": counter(100, 60)}, used_cpu=None, cache_mode="ha")
    assert "dcs_cpu_counter" not in rows[0].attrs and rows[0].metrics["cpu_usage"] is None
