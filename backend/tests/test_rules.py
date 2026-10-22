import pytest

from app.hcs.pager import page_all
from app.rules.evaluator import evaluate_rule
from app.rules.expr import ExprError, MissingFact, evaluate
from app.rules.loader import load_yaml_rules
from app.rules.model import Rule


def test_expr_basic():
    facts = {"status": "RESTARTING", "status_duration_min": 75, "used": 900, "total": 1000}
    assert evaluate("status in ('RESTARTING', 'EXTENDING') and status_duration_min > 60", facts) is True
    assert evaluate("used / total * 100", facts) == 90
    assert evaluate("max(1, used, 3)", facts) == 900


@pytest.mark.parametrize("src", ["__import__('os').system('id')", "status.upper()", "[x for x in y]",
                                 "open('/etc/passwd')", "lambda: 1"])
def test_expr_rejects_unsafe(src):
    with pytest.raises(ExprError):
        evaluate(src, {"status": "x", "y": []})


def test_expr_missing_fact():
    with pytest.raises(MissingFact):
        evaluate("memory_usage >= 90", {})


def _rule(rule_id: str) -> Rule:
    raw = next(r for r in load_yaml_rules() if r["id"] == rule_id)
    return Rule(**{"recover": {}, "for_count": 1, **raw})


def test_threshold_for_count_upgrade_and_hysteresis():
    rule, state = _rule("R-DCS-005"), {}
    assert evaluate_rule(rule, {"memory_usage": 85.0}, state, None).action is None       # 1/3
    assert evaluate_rule(rule, {"memory_usage": 86.0}, state, None).action is None       # 2/3
    d = evaluate_rule(rule, {"memory_usage": 84.6}, state, None)
    assert d.action == "FIRE" and d.match.severity == "P3" and d.match.title == "内存使用率 84.6%（≥80%）"
    d = evaluate_rule(rule, {"memory_usage": 92.3}, state, "P3")
    assert d.action == "UPGRADE" and d.match.severity == "P2" and d.match.threshold_text == "≥90%"
    assert evaluate_rule(rule, {"memory_usage": 78.0}, state, "P2").action == "REFRESH"  # 回差区间
    assert evaluate_rule(rule, {"memory_usage": 70.0}, state, "P2").action == "RESOLVE"
    assert evaluate_rule(rule, {}, state, "P2").action is None                            # 无数据不误判恢复


def test_state_rule_critical_severity():
    rule = _rule("R-DGC-BAT-001")
    assert evaluate_rule(rule, {"last_status": "fail", "critical": True}, {}, None).match.severity == "P1"
    assert evaluate_rule(rule, {"last_status": "fail", "critical": False}, {}, None).match.severity == "P2"


def test_page_all_offset_and_page_modes():
    data = list(range(250))
    got = list(page_all(lambda p: {"items": data[p["offset"]:p["offset"] + p["limit"]], "total": 250}, "items"))
    assert got == data
    calls = []

    def fetch(p):
        calls.append(p)
        start = (p["currentPage"] - 1) * p["pageSize"]
        return {"rows": data[start:start + p["pageSize"]]}

    got = list(page_all(fetch, "rows", mode="page", start=1, limit=100, offset_param="currentPage",
                        limit_param="pageSize"))
    assert got == data and len(calls) == 3
