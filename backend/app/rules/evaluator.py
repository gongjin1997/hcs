"""规则评估：纯函数，输入（规则, 本次事实, 历史评估状态, 当前活动告警级别），输出状态机动作。

动作：FIRE（新告警）/ UPGRADE（升级）/ DOWNGRADE（降级）/ REFRESH（持续中）/ RESOLVE（恢复）/ None（无变化）
"""
import logging
from dataclasses import dataclass
from threading import Lock
from time import monotonic

from .expr import ExprError, MissingFact
from .model import SEVERITY_RANK, Match, Rule

log = logging.getLogger(__name__)
_error_times: dict[tuple[str, str], float] = {}
_error_guard = Lock()


def _log_rule_failure(rule: Rule, exc: Exception, phase: str) -> None:
    now = monotonic()
    key = (rule.id, phase)
    with _error_guard:
        previous = _error_times.get(key)
        if previous is not None and now - previous < 60:
            return
        if len(_error_times) >= 1024:
            _error_times.clear()
        _error_times[key] = now
    log.warning("rule_evaluation_failed", extra={"event": "rule_evaluation_failed", "rule_id": rule.id,
                "resource_type": rule.resource_type, "check_item": rule.check_item, "phase": phase,
                "exception_type": type(exc).__name__},
                exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))


@dataclass
class Decision:
    action: str | None
    match: Match | None = None
    message: str = ""
    data_available: bool = True


def evaluate_rule(rule: Rule, facts: dict, state: dict, active_severity: str | None) -> Decision:
    """state 为该指纹的评估状态（原地更新）：hits 连续命中次数，ok_hits 连续恢复次数。"""
    try:
        m = rule.match(facts)
    except MissingFact as e:
        state["hits"] = state["ok_hits"] = 0
        log.debug("rule_data_unavailable", extra={"event": "rule_data_unavailable", "rule_id": rule.id})
        return Decision(None, message=f"无数据，保持现状（{e}）", data_available=False)
    except ExprError as e:
        state["hits"] = state["ok_hits"] = 0
        _log_rule_failure(rule, e, "match")
        return Decision(None, message=f"规则表达式错误：{e}", data_available=False)

    if m:
        state["hits"] = state.get("hits", 0) + 1
        state["ok_hits"] = 0
        if active_severity is None:
            if state["hits"] < rule.for_count:
                return Decision(None, m, f"命中 {state['hits']}/{rule.for_count} 次，等待确认")
            return Decision("FIRE", m, "触发告警")
        diff = SEVERITY_RANK[m.severity] - SEVERITY_RANK[active_severity]
        if diff < 0:
            return Decision("UPGRADE", m, f"{active_severity} → {m.severity}")
        if diff > 0:
            return Decision("DOWNGRADE", m, f"{active_severity} → {m.severity}")
        return Decision("REFRESH", m, "告警持续")

    state["hits"] = 0
    if active_severity is None:
        state["ok_hits"] = 0
        return Decision(None, message="正常")
    try:
        ok = rule.recovered(facts)
    except ExprError as e:
        state["ok_hits"] = 0
        if not isinstance(e, MissingFact):
            _log_rule_failure(rule, e, "recover")
        return Decision(None, message=f"恢复条件无法判定：{e}", data_available=False)
    if not ok:
        state["ok_hits"] = 0
        return Decision("REFRESH", None, "处于回差区间，保持告警")
    state["ok_hits"] = state.get("ok_hits", 0) + 1
    need = int(rule.recover.get("for_count", 1))
    if state["ok_hits"] < need:
        return Decision(None, message=f"恢复确认中 {state['ok_hits']}/{need}")
    return Decision("RESOLVE", None, "已恢复")
