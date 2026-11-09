"""采集范围匹配引擎（app.collectors.scope）：通配、三值判定、对象 → 维度取值。纯函数，不依赖数据库。"""
import time

import pytest

from app.collectors.scope import (
    OPEN,
    compile_scope,
    glob_match,
    ledger_subject,
    pattern_matches,
    subject_of,
)


def scope(*rules):
    return compile_scope({"rules": list(rules)})


def exclude(**match):
    return {"effect": "exclude", "match": {k: v if isinstance(v, list) else [v] for k, v in match.items()}}


def include(**match):
    return {"effect": "include", "match": {k: v if isinstance(v, list) else [v] for k, v in match.items()}}


@pytest.mark.parametrize("pattern,value,expected", [
    ("a*b", "axxb", True), ("a*b", "axxbc", False), ("*", "anything", True), ("a?c", "abc", True),
    ("a?c", "ac", False), ("*-prod", "redis-prod", True), ("redis-*-01", "redis-cache-01", True),
    ("redis-*-01", "redis-cache-02", False), ("**", "x", True), ("a*", "a", True),
    ("[x]*", "[x]yz", True), ("[x]*", "xyz", False),            # 方括号按字面处理，不是字符集
])
def test_glob_wildcards_are_star_and_question_mark_only(pattern, value, expected):
    assert glob_match(pattern, value) is expected


def test_exact_values_are_case_sensitive_and_empty_values_never_match():
    assert pattern_matches("prod", "prod") and not pattern_matches("prod", "Prod")
    assert not pattern_matches("*", "") and not pattern_matches("", "")


def test_pathological_wildcards_do_not_backtrack_exponentially():
    started = time.perf_counter()
    assert glob_match("*a" * 12 + "b", "a" * 255) is False
    assert time.perf_counter() - started < 1


def test_no_rules_means_everything_is_in_scope():
    assert OPEN.open and OPEN.check({"name": "x"}) is True and compile_scope(None).open
    assert compile_scope({"rules": []}).open and compile_scope({"rules": "bad"}).open


def test_exclude_rules_remove_matching_objects_and_keep_the_rest():
    flt = scope(exclude(namespace=["test-*", "dev"]))
    assert flt.check({"namespace": "test-1"}) is False and flt.check({"namespace": "dev"}) is False
    assert flt.check({"namespace": "prod"}) is True


def test_include_rules_keep_only_matching_objects():
    flt = scope(include(namespace="prod"))
    assert flt.check({"namespace": "prod"}) is True and flt.check({"namespace": "dev"}) is False


def test_exclude_wins_over_include_and_dimensions_are_anded():
    flt = scope(include(workspace="ws1"), exclude(workspace="ws1", job="tmp_*"))
    assert flt.check({"workspace": "ws1", "job": "etl"}) is True
    assert flt.check({"workspace": "ws1", "job": "tmp_a"}) is False
    assert flt.check({"workspace": "ws2", "job": "etl"}) is False
    two = scope(include(workspace="ws1", job="etl*"))
    assert two.check({"workspace": "ws1", "job": "etl_a"}) is True
    assert two.check({"workspace": "ws1", "job": "other"}) is False


def test_several_include_rules_are_ored():
    flt = scope(include(workspace="ws1"), include(workspace="ws2", job="a*"))
    assert flt.check({"workspace": "ws1", "job": "x"}) is True
    assert flt.check({"workspace": "ws2", "job": "abc"}) is True
    assert flt.check({"workspace": "ws2", "job": "zzz"}) is False


def test_missing_dimension_is_unknown_not_false_so_collectors_keep_the_object():
    flt = scope(exclude(namespace="test-*", workload="tmp-*"))
    assert flt.check({"namespace": "test-1", "workload": None}) is None
    assert flt.keep({"namespace": "test-1"}) is True                         # 还不知道工作负载：保留，流水线再判定
    assert flt.check({"namespace": "test-1", "workload": "tmp-a"}) is False
    assert flt.check({"namespace": "prod"}) is True                          # 命名空间已不匹配，不需要工作负载


def test_a_whole_workspace_can_be_excluded_before_listing_its_jobs():
    flt = scope(exclude(workspace="ws9"))
    assert flt.check({"workspace": "ws9", "job": None}) is False
    assert flt.keep({"workspace": "ws1"}) is True
    inc = scope(include(workspace="ws1", job="a*"))
    assert inc.keep({"workspace": "ws2"}) is False                           # 其他工作空间整体在范围外
    assert inc.check({"workspace": "ws1"}) is None                           # 本空间内要看作业名


def test_tuple_values_match_cluster_name_or_id():
    by_name = scope(include(cluster="prod-1"))
    assert by_name.check({"cluster": ("uid-1", "prod-1")}) is True
    assert by_name.check({"cluster": ("uid-1", "dev")}) is False
    by_id = scope(include(cluster="uid-1"))
    assert by_id.check({"cluster": ("uid-1", "")}) is True


def test_ledger_dimensions_treat_unassigned_as_not_matching():
    flt = scope(include(business="*"))
    assert flt.check(ledger_subject(business="pay")) is True
    assert flt.check(ledger_subject()) is False                              # 业务系统为空不匹配 *
    crit = scope(include(critical="true"))
    assert crit.check(ledger_subject(critical=True)) is True and crit.check(ledger_subject(critical=False)) is False


def test_malformed_stored_rules_are_dropped_instead_of_widening_or_crashing():
    flt = compile_scope({"rules": [{"effect": "x", "match": {"a": ["1"]}}, {"effect": "include", "match": {"a": []}},
                                   {"effect": "include", "match": "no"}, 5, None,
                                   {"effect": "exclude", "match": {"namespace": ["kept"]}}]})
    assert len(flt.rules) == 1 and flt.check({"namespace": "kept"}) is False


def test_subjects_for_each_resource_family():
    assert subject_of("dcs_instance", "i-1", "redis", {}) == {"id": "i-1", "name": "redis"}
    assert subject_of("dgc_batch_job", "ws/j1", "j1", {"workspace": "ws"}) == {"workspace": "ws", "job": "j1"}
    assert subject_of("dgc_realtime_job", "j1", "j1", {"workspace": ""}) == {"workspace": "", "job": "j1"}
    assert subject_of("dgc_mrs_node", "ws/j/n", "j / n", {"workspace": "ws", "job_name": "j", "node_name": "n"}) == {
        "workspace": "ws", "job": "j", "node": "n"}
    assert subject_of("cce_cluster", "uid", "cce-a", {}) == {"cluster": ("uid", "cce-a")}
    workload = subject_of("cce_workload", "cid/ns1/ReplicaSet/web-5d", "ns1/web-5d",
                          {"cluster": "c1", "cluster_id": "cid", "namespace": "ns1", "workload": "web-5d"})
    assert workload == {"cluster": ("cid", "c1"), "namespace": "ns1", "workload": "web-5d"}
    # 观测里没有属性时回退到资源 ID：集群ID/命名空间/类型/名称
    assert subject_of("cce_workload", "cid/ns1/ReplicaSet/web-5d", "ns1/web-5d", {}) == {
        "cluster": ("cid", ""), "namespace": "ns1", "workload": "web-5d"}
