"""规则评估：纯函数，输入（规则, 本次事实, 历史评估状态, 当前活动告警级别），输出状态机动作。

动作：FIRE（新告警）/ UPGRADE（升级）/ DOWNGRADE（降级）/ REFRESH（持续中）/ RESOLVE（恢复）/ None（无变化）
"""
from dataclasses import dataclass

from .expr import ExprError, MissingFact
from .model import SEVERITY_RANK, Match, Rule


@dataclass
class Decision:
    action: str | None
    match: Match | None = None
    message: str = ""


def evaluate_rule(rule: Rule, facts: dict, state: dict, active_severity: str | None) -> Decision:
    """state 为该指纹的评估状态（原地更新）：hits 连续命中次数，ok_hits 连续恢复次数。"""
    try:
        m = rule.match(facts)
    except MissingFact as e:
        return Decision(None, message=f"无数据，保持现状（{e}）")
    except ExprError as e:
        return Decision(None, message=f"规则表达式错误：{e}")

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
        return Decision(None, message="正常")
    try:
        ok = rule.recovered(facts)
    except ExprError as e:
        return Decision(None, message=f"恢复条件无法判定：{e}")
    if not ok:
        return Decision("REFRESH", None, "处于回差区间，保持告警")
    state["ok_hits"] = state.get("ok_hits", 0) + 1
    need = int(rule.recover.get("for_count", 1))
    if state["ok_hits"] < need:
        return Decision(None, message=f"恢复确认中 {state['ok_hits']}/{need}")
    return Decision("RESOLVE", None, "已恢复")
