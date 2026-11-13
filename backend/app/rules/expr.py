"""安全表达式求值：基于 AST 白名单，只允许比较、布尔、算术、in、常量、少量内置函数。

示例：status == 'EXCEPTION'
      status in ('RESTARTING', 'EXTENDING') and status_duration_min > 60
      used_memory_mb / max_memory_mb * 100
"""
import ast
import math
import operator as op
from collections.abc import Callable
from functools import lru_cache
from typing import Any


class ExprError(ValueError):
    pass


class MissingFact(ExprError):
    """表达式引用的事实本次未采集到（无数据），评估器据此跳过而不是误判恢复。"""


_FUNCS: dict[str, Callable[..., Any]] = {"abs": abs, "min": min, "max": max, "len": len, "round": round}
_BIN: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv, ast.Mod: op.mod}
_CMP: dict[type[ast.cmpop], Callable[[Any, Any], Any]] = {
    ast.Eq: op.eq, ast.NotEq: op.ne, ast.Lt: op.lt, ast.LtE: op.le, ast.Gt: op.gt, ast.GtE: op.ge,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}
_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.BinOp,
            ast.Compare, ast.Call, ast.Name, ast.Load, ast.Constant, ast.Tuple, ast.List, ast.IfExp,
            *_BIN.keys(), *_CMP.keys())


@lru_cache(maxsize=1024)
def compile_expr(src: str) -> ast.Expression:
    if len(src) > 500:
        raise ExprError("表达式过长（上限 500 字符）")
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"语法错误：{e.msg}") from e
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ExprError(f"不允许的语法：{type(node).__name__}")
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS
                                           or node.keywords):
            raise ExprError("只允许调用 abs / min / max / len / round")
    return tree


def names(src: str) -> set[str]:
    """表达式引用的变量名（不含函数名），用于规则编辑器提示。"""
    tree = compile_expr(src)
    funcs = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - funcs


def _finite(value: Any) -> Any:
    # 比较会将 NaN/Infinity 转成 bool，不能只检查最终表达式的返回类型。
    if isinstance(value, float) and not math.isfinite(value):
        raise MissingFact("数值不是有限数")
    return value


def evaluate(src: str, facts: dict[str, Any]) -> Any:
    try:
        result = _eval(compile_expr(src).body, facts)
        if isinstance(result, float) and not math.isfinite(result):
            raise MissingFact("数值不是有限数")
        return result
    except TypeError as e:            # 例如 None >= 90
        raise ExprError(f"类型不匹配：{e}") from e
    except ZeroDivisionError as e:
        raise MissingFact("除数为 0") from e
    except ExprError:
        raise
    except (ValueError, OverflowError) as e:
        raise ExprError(f"数值或函数参数无效：{e}") from e


def _eval(node: ast.AST, facts: dict[str, Any]) -> Any:
    # 栈深约束：表达式最长 500 个字符，求值占用的栈帧数不能超过字符数。连续负号一个字符一层嵌套，
    # 所以负号、算术等必须在本函数里直接递归；and / or 与比较每层至少多出两个字符，才能拆到辅助函数（每层两帧）。
    # 不要改成“按节点类型查表再调用处理函数”：500 个负号会超过默认递归上限，而 RecursionError 不会转成 ExprError。
    if isinstance(node, ast.Constant):
        return _finite(node.value)
    if isinstance(node, ast.Name):
        return _fact(node.id, facts)
    if isinstance(node, ast.BoolOp):
        return _bool_op(node, facts)
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, facts)
        return (not operand) if isinstance(node.op, ast.Not) else -operand
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, facts), _eval(node.right, facts)
        if type(left) not in (int, float) or type(right) not in (int, float):
            raise ExprError("算术运算只支持数值")
        return _finite(_BIN[type(node.op)](left, right))
    if isinstance(node, ast.Compare):
        return _compare(node, facts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        args = [_eval(a, facts) for a in node.args]
        if node.func.id == "round" and len(args) == 2 and isinstance(args[1], int):
            if not (-100 <= args[1] <= 100):
                raise ExprError("round 精度参数必须是 -100 到 100 之间的整数")
        return _finite(_FUNCS[node.func.id](*args))
    if isinstance(node, ast.Tuple | ast.List):
        return tuple(_eval(e, facts) for e in node.elts)
    if isinstance(node, ast.IfExp):
        return _eval(node.body, facts) if _eval(node.test, facts) else _eval(node.orelse, facts)
    raise ExprError(f"不支持的表达式节点：{type(node).__name__}")


def _fact(name: str, facts: dict[str, Any]) -> Any:
    if facts.get(name) is None:
        raise MissingFact(f"缺少数据：{name}")
    return _finite(facts[name])


def _bool_op(node: ast.BoolOp, facts: dict[str, Any]) -> Any:
    """and 遇假即停、or 遇真即停，返回值语义与 Python 一致。"""
    is_and = isinstance(node.op, ast.And)
    result: Any = is_and
    for v in node.values:
        result = _eval(v, facts)
        if bool(result) != is_and:
            return result
    return result


def _compare(node: ast.Compare, facts: dict[str, Any]) -> bool:
    """链式比较从左到右求值，遇到不成立即停。"""
    left = _eval(node.left, facts)
    for cmp_op, comp in zip(node.ops, node.comparators, strict=True):
        right = _eval(comp, facts)
        if not _CMP[type(cmp_op)](left, right):
            return False
        left = right
    return True
