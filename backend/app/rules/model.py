"""规则模型：threshold（多级阈值）与 state（状态匹配）两类，覆盖巡检 90% 的场景。

threshold：expr = {"value": "memory_usage", "unit": "%", "levels": [{"severity": "P1", "op": ">=", "threshold": 95}, ...],
                   "title": "内存使用率 {value}（{threshold}）"}；recover = {"below": 75}
state：    expr = {"when": "status == 'ERROR'", "severity": "P1", "critical_severity": "P1", "title": "..."}；
           recover = {"when": "status == 'RUNNING'"}（缺省为“条件不再满足”）
"""
from dataclasses import dataclass, field
from typing import Any

from .expr import ExprError, evaluate

SEVERITY_RANK = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
_OPS = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b, "<": lambda a, b: a < b}
_OP_TEXT = {">=": "≥", ">": ">", "<=": "≤", "<": "<"}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "-"


def render_title(tpl: str, facts: dict, **extra: Any) -> str:
    """标题模板渲染：{字段} 取观测值，缺失的字段显示为 “-”。"""
    return tpl.format_map(_SafeDict({**facts, **extra}))


@dataclass
class Match:
    severity: str
    value: Any = None
    current_value: str | None = None
    threshold_text: str | None = None
    title: str = ""


@dataclass
class Rule:
    id: str
    name: str
    resource_type: str
    check_item: str
    kind: str
    expr: dict
    for_count: int = 1
    recover: dict = field(default_factory=dict)
    scope: dict = field(default_factory=dict)
    repeat_interval_sec: int | None = None
    runbook: str | None = None
    enabled: bool = True

    @classmethod
    def from_obj(cls, o: Any) -> "Rule":
        return cls(**{k: getattr(o, k) for k in cls.__dataclass_fields__})

    # ---- 匹配 ----
    def match(self, facts: dict) -> Match | None:
        if self.kind == "threshold":
            return self._match_threshold(facts)
        if self.kind == "state":
            return self._match_state(facts)
        raise ExprError(f"暂不支持的规则类型：{self.kind}")

    def _fmt(self, v: Any) -> str:
        unit = self.expr.get("unit", "")
        return f"{round(v, 1) if isinstance(v, float) else v}{unit}"

    def _title(self, facts: dict, **extra: Any) -> str:
        return render_title(self.expr.get("title") or self.name, facts, **extra)

    def _match_threshold(self, facts: dict) -> Match | None:
        value = evaluate(self.expr["value"], facts)
        levels = sorted(self.expr.get("levels", []), key=lambda lv: SEVERITY_RANK[lv["severity"]])
        for lv in levels:  # 从最高级别开始，命中即返回
            if _OPS[lv.get("op", ">=")](value, lv["threshold"]):
                cur = self._fmt(value)
                thr = f"{_OP_TEXT[lv.get('op', '>=')]}{self._fmt(lv['threshold'])}"
                return Match(lv["severity"], value, cur, thr, self._title(facts, value=cur, threshold=thr))
        return None

    def _match_state(self, facts: dict) -> Match | None:
        if not evaluate(self.expr["when"], facts):
            return None
        sev = self.expr["severity"]
        if facts.get("critical") and self.expr.get("critical_severity"):
            sev = self.expr["critical_severity"]
        cur = str(facts.get(self.expr.get("value_field", "status"), "")) or None
        return Match(sev, cur, cur, self.expr.get("threshold_text"), self._title(facts, value=cur))

    # ---- 恢复判定（滞回：恢复阈值低于触发阈值，避免在阈值附近反复告警/恢复）----
    def recovered(self, facts: dict) -> bool:
        if self.recover.get("when"):
            return bool(evaluate(self.recover["when"], facts))
        if self.kind == "threshold" and "below" in self.recover:
            return evaluate(self.expr["value"], facts) < self.recover["below"]
        return self.match(facts) is None
