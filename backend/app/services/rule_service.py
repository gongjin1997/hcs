"""自定义规则校验；目标和字段来自资源指南，表达式只使用既有 AST 白名单。"""
import ast
import math
from collections.abc import Mapping
from string import Formatter

from app.core.response import BizError, ErrorCode
from app.rules.expr import ExprError, compile_expr, names
from app.rules.loader import load_yaml_rules
from app.schemas.rule import RuleIn, RuleTargetOut


def list_rule_targets() -> list[RuleTargetOut]:
    from app.services.resource_guides import list_resource_guides, rule_fact_fields

    baselines = load_yaml_rules()
    targets = []
    for guide in list_resource_guides():
        checks = sorted({check for field in guide.facts for check in field.check_items})
        for check in checks:
            fields = rule_fact_fields(guide.resource_type, check)
            examples = [{"kind": row["kind"], "expr": row["expr"], "recover": row.get("recover", {})}
                        for row in baselines if (row["resource_type"], row["check_item"]) == (guide.resource_type, check)]
            targets.append(RuleTargetOut(resource_type=guide.resource_type, name=guide.name, service=guide.service,
                                         check_item=check, collection_tasks=guide.collection_tasks, facts=fields,
                                         expression_examples=examples, limitations=guide.limitations,
                                         collection_available=any(field.availability != "not_collected"
                                             and check in field.check_items for field in fields)))
    return targets


def _expression_type(node: ast.AST, fields: Mapping[str, str]) -> str:  # noqa: C901  # T10
    """只推导类型，不执行用户表达式；避免通过算术创建巨型字符串或集合。"""
    numeric = {"number", "integer"}
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return "boolean"
        if isinstance(node.value, int | float):
            if isinstance(node.value, float) and not math.isfinite(node.value):
                raise ExprError("数值常量必须是有限数")
            return "number"
        if isinstance(node.value, str) and len(node.value) <= 256:
            return "string"
        raise ExprError("只支持数值、布尔与不超过 256 字符的字符串常量")
    if isinstance(node, ast.Name):
        return fields[node.id]
    if isinstance(node, ast.List | ast.Tuple):
        for element in node.elts:
            _expression_type(element, fields)
        return "array"
    if isinstance(node, ast.UnaryOp):
        operand = _expression_type(node.operand, fields)
        if isinstance(node.op, ast.Not):
            return "boolean"
        if operand not in numeric:
            raise ExprError("取负只支持数值字段")
        return "number"
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Div | ast.Mod) and isinstance(node.right, ast.Constant) and node.right.value == 0:
            raise ExprError("除数不能恒为 0")
        if _expression_type(node.left, fields) not in numeric or _expression_type(node.right, fields) not in numeric:
            raise ExprError("算术运算只支持数值字段")
        return "number"
    if isinstance(node, ast.Compare):
        left = _expression_type(node.left, fields)
        for operator, right_node in zip(node.ops, node.comparators, strict=True):
            right = _expression_type(right_node, fields)
            if isinstance(operator, ast.In | ast.NotIn):
                if right not in {"array", "string", "object"}:
                    raise ExprError("in / not in 右侧必须是集合或字符串")
                if right == "string" and left != "string":
                    raise ExprError("字符串包含判断的左侧必须是字符串")
                if isinstance(right_node, ast.Tuple | ast.List):
                    for element in right_node.elts:
                        other = _expression_type(element, fields)
                        if other != left and not {other, left} <= numeric:
                            raise ExprError("集合元素与比较字段类型不一致")
            elif left != right and not {left, right} <= numeric:
                raise ExprError("比较字段类型不一致")
            elif isinstance(operator, ast.Lt | ast.LtE | ast.Gt | ast.GtE) and left not in numeric | {"string"}:
                raise ExprError("大小比较只支持数值或字符串")
            left = right
        return "boolean"
    if isinstance(node, ast.BoolOp):
        if any(_expression_type(value, fields) != "boolean" for value in node.values):
            raise ExprError("and / or 两侧必须是布尔条件")
        return "boolean"
    if isinstance(node, ast.IfExp):
        _expression_type(node.test, fields)
        first, second = _expression_type(node.body, fields), _expression_type(node.orelse, fields)
        if first != second and not {first, second} <= numeric:
            raise ExprError("条件表达式的分支类型不一致")
        return "number" if first in numeric else first
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        args = [_expression_type(arg, fields) for arg in node.args]
        if node.func.id == "len":
            if len(args) != 1 or args[0] not in {"array", "string", "object"}:
                raise ExprError("len 需要一个字符串或集合参数")
        elif node.func.id in {"min", "max"}:
            if len(args) < 2 or any(arg not in numeric for arg in args):
                raise ExprError("min / max 需要至少两个数值参数")
        elif node.func.id == "abs":
            if len(args) != 1 or args[0] not in numeric:
                raise ExprError("abs 需要一个数值参数")
        elif (len(args) not in {1, 2} or any(arg not in numeric for arg in args)
              or (len(args) == 2 and not (isinstance(node.args[1], ast.Constant)
                                        and type(node.args[1].value) is int
                                        and -100 <= node.args[1].value <= 100))):
            raise ExprError("round 需要一个数值参数及可选的整数精度（-100 至 100）")
        return "number"
    raise ExprError("不支持的表达式类型")


def validate_rule(body: RuleIn, resource_type: str | None = None, check_item: str | None = None, *,  # noqa: C901  # T10
                  allow_unavailable: bool = False) -> None:
    try:
        target = None
        if resource_type is not None:
            target = next((item for item in list_rule_targets()
                           if (item.resource_type, item.check_item) == (resource_type, check_item)), None)
            if target is None:
                raise ExprError("资源类型与检查项不匹配，或该类型尚无实际巡检采集器")
            if not target.collection_available and not allow_unavailable:
                raise ExprError("该检查项当前未采集，不能启用规则；历史规则可停用保留")
        fields = {field.key: field.data_type for field in target.facts} if target else None
        expressions = [(body.expr["value" if body.kind == "threshold" else "when"],
                        "number" if body.kind == "threshold" else "boolean")]
        if body.recover.get("when"):
            expressions.append((body.recover["when"], "boolean"))
        for expression, expected_type in expressions:
            tree = compile_expr(expression)
            if sum(1 for _ in ast.walk(tree)) > 128:
                raise ExprError("表达式过于复杂（上限 128 个 AST 节点）")
            if fields is not None:
                referenced = names(expression)
                if not referenced:
                    raise ExprError("表达式至少引用一个可用事实字段")
                unknown = referenced - fields.keys()
                if unknown:
                    raise ExprError(f"该检查项没有这些事实字段：{', '.join(sorted(unknown))}")
                result_type = _expression_type(tree.body, fields)
                if result_type != expected_type and not (expected_type == "number" and result_type == "integer"):
                    raise ExprError(f"表达式结果必须是 {expected_type}")
        if fields is not None:
            value_field = body.expr.get("value_field")
            if value_field and value_field not in fields:
                raise ExprError(f"展示值字段不可用：{value_field}")
        for _, field_name, format_spec, conversion in Formatter().parse(body.expr.get("title", "")):
            if field_name is None:
                continue
            if (not field_name.isidentifier() or format_spec or conversion
                    or (fields is not None and field_name not in fields.keys() | {"value", "threshold"})):
                raise ExprError("标题仅支持可用字段、{value}、{threshold} 简单占位符")
    except (ExprError, ValueError, TypeError, RecursionError) as exc:
        raise BizError(ErrorCode.BAD_REQUEST, str(exc)) from exc
