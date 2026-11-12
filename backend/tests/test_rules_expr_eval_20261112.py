"""rules/expr.py 求值器的特征测试（T10）：固定输入 → 固定输出，拆分 `_eval` 前后都必须通过。"""
import ast
import math
import sys

import pytest

from app.rules import expr
from app.rules.expr import ExprError, MissingFact, compile_expr, evaluate

FACTS = {"status": "RUNNING", "n": 7, "zero": 0, "ratio": 2.5, "flag": True, "off": False, "empty": "", "none": None,
         "inf": math.inf, "nan": math.nan, "items": ["a", "b"], "big": 1e308}
ONLY_FIVE = "只允许调用 abs / min / max / len / round"

VALUES = [
    # 常量与变量
    ("1", 1), ("'a'", "a"), ("True", True), ("None", None), ("n", 7), ("status", "RUNNING"), ("empty", ""), ("zero", 0),
    # and / or 返回决定结果的那个操作数，不转成布尔值
    ("n and status", "RUNNING"), ("zero and missing", 0), ("flag or missing", True), ("off or empty", ""),
    ("zero or off or n", 7), ("n and ratio and empty", ""),
    # 一元运算
    ("not zero", True), ("not status", False), ("-n", -7), ("-ratio", -2.5), ("-flag", -1),
    # 算术
    ("n + 1", 8), ("n - 10", -3), ("n * ratio", 17.5), ("n / 2", 3.5), ("n % 4", 3), ("n / (zero or 2)", 3.5),
    # 比较：链式比较遇到不成立即停
    ("n > 5", True), ("1 < n < 10", True), ("1 < n < 5", False), ("10 < n < missing", False), ("n <= 7 >= 7", True),
    ("status == 'RUNNING'", True), ("status != 'RUNNING'", False), ("status in ('RUNNING', 'DOWN')", True),
    ("status not in ('RUNNING', 'DOWN')", False), ("status in items", False), ("'a' in items", True),
    ("ratio == 2.5", True),
    # 函数
    ("abs(-n)", 7), ("min(n, 3)", 3), ("max(1, n, 3)", 7), ("len(status)", 7), ("len(items)", 2), ("round(ratio)", 2),
    ("round(n / 3, 2)", 2.33),
    # 序列一律求值为元组；条件表达式只求值被选中的分支
    ("(1, n, status)", (1, 7, "RUNNING")), ("[1, n]", (1, 7)), ("()", ()), ("n if flag else missing", 7),
    ("missing if off else n", 7), ("'big' if n > 5 else 'small'", "big"), ("(n if n > 5 else 0) + 1", 8),
    # 组合
    ("status in ('RESTARTING', 'RUNNING') and n > 5", True), ("max(n, 3) * 2 > 10 and not off", True),
]

# 无数据（MissingFact）与规则写错（ExprError）要分清：评估器对前者跳过，对后者报规则错误。
ERRORS = [
    ("missing", MissingFact, "缺少数据：missing"), ("none", MissingFact, "缺少数据：none"),
    ("n and missing", MissingFact, "缺少数据：missing"), ("off or missing", MissingFact, "缺少数据：missing"),
    ("not missing", MissingFact, "缺少数据：missing"), ("-missing", MissingFact, "缺少数据：missing"),
    ("1 < n < missing", MissingFact, "缺少数据：missing"), ("abs(missing)", MissingFact, "缺少数据：missing"),
    ("(missing,)", MissingFact, "缺少数据：missing"), ("missing if flag else n", MissingFact, "缺少数据：missing"),
    ("n if missing else 1", MissingFact, "缺少数据：missing"), ("status + missing", MissingFact, "缺少数据：missing"),
    ("inf", MissingFact, "数值不是有限数"), ("nan", MissingFact, "数值不是有限数"), ("1e999", MissingFact, "数值不是有限数"),
    ("big * 10", MissingFact, "数值不是有限数"), ("n / zero", MissingFact, "除数为 0"), ("n % zero", MissingFact, "除数为 0"),
    ("status + 'x'", ExprError, "算术运算只支持数值"), ("flag + 1", ExprError, "算术运算只支持数值"),
    ("+n", ExprError, "不允许的语法：UAdd"), ("n ** 2", ExprError, "不允许的语法：Pow"),
    ("n // 2", ExprError, "不允许的语法：FloorDiv"), ("int(n)", ExprError, ONLY_FIVE),
    ("max(n, key=n)", ExprError, ONLY_FIVE), ("a" * 501, ExprError, "表达式过长（上限 500 字符）"),
]

# 这些报错的后半句是解释器给的原文，只固定前缀。
PYTHON_ERRORS = [
    ("-status", "类型不匹配："), ("status >= 90", "类型不匹配："), ("n in status", "类型不匹配："), ("len(n)", "类型不匹配："),
    ("max()", "类型不匹配："), ("max(n, status)", "类型不匹配："), ("round(ratio, 'x')", "类型不匹配："),
    ("len(status, status)", "类型不匹配："), ("min(())", "数值或函数参数无效："),
    ("1" + "0" * 400 + " * 1.5", "数值或函数参数无效："), ("1 +", "语法错误："), ("n; n", "语法错误："),
]

READS = [
    ("zero and n", ["zero"]), ("n and zero and status", ["n", "zero"]), ("flag or n", ["flag"]),
    ("off or zero or n", ["off", "zero", "n"]), ("1 < zero < n", ["zero"]), ("zero < n < ratio", ["zero", "n", "ratio"]),
    ("n if flag else status", ["flag", "n"]), ("n if off else status", ["off", "status"]),
    ("min(n, zero, ratio)", ["n", "zero", "ratio"]), ("n + zero", ["n", "zero"]),
    ("(n, zero, status)", ["n", "zero", "status"]), ("status != 'RUNNING' and n > missing", ["status"]),
]
READS_BEFORE_MISSING = [
    ("status + missing", ["status", "missing"]), ("missing + status", ["missing"]),
    ("max(n, missing, zero)", ["n", "missing"]), ("n in (zero, missing)", ["n", "zero", "missing"]),
    ("status == 'RUNNING' and n > missing", ["status", "n", "missing"]),
]

STACK_BUDGET = 520  # 表达式上限 500 个字符；嵌套最深的写法每个字符占一层栈帧


class Recorder(dict):
    """记录求值过程中读取了哪些事实、按什么顺序。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read: list[str] = []

    def get(self, key, default=None):
        self.read.append(key)
        return super().get(key, default)


def stack_depth() -> int:
    frame, depth = sys._getframe(), 0
    while frame is not None:
        frame, depth = frame.f_back, depth + 1
    return depth


def evaluate_within_budget(src: str):
    assert len(src) <= 500
    compile_expr(src)  # 语法树只构建一次并缓存，不计入求值的栈预算
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(stack_depth() + STACK_BUDGET)
    try:
        return evaluate(src, {})
    finally:
        sys.setrecursionlimit(limit)


@pytest.mark.parametrize("src, expected", VALUES)
def test_value_and_type_of_each_kind_of_expression(src, expected):
    result = evaluate(src, FACTS)
    assert type(result) is type(expected) and result == expected


@pytest.mark.parametrize("src, kind, message", ERRORS)
def test_missing_data_and_invalid_expressions_raise_distinct_errors(src, kind, message):
    with pytest.raises(ExprError) as caught:
        evaluate(src, FACTS)
    assert type(caught.value) is kind and str(caught.value) == message


@pytest.mark.parametrize("src, prefix", PYTHON_ERRORS)
def test_interpreter_errors_are_reported_as_invalid_expressions(src, prefix):
    with pytest.raises(ExprError) as caught:
        evaluate(src, FACTS)
    assert type(caught.value) is ExprError and str(caught.value).startswith(prefix)


@pytest.mark.parametrize("src, read", READS)
def test_facts_are_read_left_to_right_and_only_when_needed(src, read):
    facts = Recorder(FACTS)
    evaluate(src, facts)
    assert facts.read == read


@pytest.mark.parametrize("src, read", READS_BEFORE_MISSING)
def test_evaluation_stops_at_the_first_missing_fact(src, read):
    facts = Recorder(FACTS)
    with pytest.raises(MissingFact, match="缺少数据：missing"):
        evaluate(src, facts)
    assert facts.read == read


@pytest.mark.parametrize("src, node", [("x.y", "Attribute"), ("a.b()", "Call"), ("{1: 2}", "Dict")])
def test_nodes_outside_the_whitelist_are_rejected_even_when_compilation_is_bypassed(src, node):
    """compile_expr 的白名单之外还有一道：直接把未知节点交给求值器也只报错，不执行。"""
    with pytest.raises(ExprError) as caught:
        expr._eval(ast.parse(src, mode="eval").body, {"x": 1, "a": 1})
    assert str(caught.value) == f"不支持的表达式节点：{node}"


@pytest.mark.parametrize("src, expected", [
    ("-" * 499 + "1", -1), ("not " * 124 + "1", True), ("1" + "+1" * 249, 250), ("abs(" * 99 + "1" + ")" * 99, 1),
    ("1<(" * 124 + "1" + ")" * 124, False), ("1 and(" * 71 + "1" + ")" * 71, 1), ("1 if 0 else " * 41 + "2", 2),
])
def test_deepest_expressions_within_the_length_limit_fit_the_stack_budget(src, expected):
    """长度上限内嵌套最深的是负号链：每多一个字符多一层嵌套，求值时每层只占一个栈帧。

    若改成“按节点类型查表，再调用处理函数”，每层会占两个栈帧，500 个字符的负号链就超过默认的 1000 层递归上限，
    而 RecursionError 不会被 evaluate 转成 ExprError。
    """
    result = evaluate_within_budget(src)
    assert type(result) is type(expected) and result == expected


@pytest.mark.parametrize("opening, levels", [("[", 199), ("[0 or ", 71)])
def test_deeply_nested_lists_fit_the_stack_budget(opening, levels):
    """解析器最多允许 200 层括号；列表每层占两个栈帧（生成器表达式也占一层），平均每个字符仍不超过一个栈帧。"""
    result, depth = evaluate_within_budget(opening * levels + "1" + "]" * levels), 0
    while isinstance(result, tuple):
        (result,), depth = result, depth + 1
    assert (result, depth) == (1, levels)


@pytest.mark.parametrize("src", ["-[" * 166 + "1" + "]" * 166, "[1<" * 124 + "1" + "]" * 124])
def test_type_errors_deep_inside_an_expression_are_reported_within_the_stack_budget(src):
    with pytest.raises(ExprError, match="类型不匹配："):
        evaluate_within_budget(src)
