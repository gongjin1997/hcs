from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_auditor
from app.core.db import get_db
from app.core.deps import CurrentUser, require_perm
from app.core.response import ApiResponse, BizError, ErrorCode, not_found, ok
from app.models import AlarmRule
from app.rules.evaluator import evaluate_rule
from app.rules.expr import ExprError, compile_expr
from app.rules.model import Rule
from app.schemas.rule import DryRunIn, DryRunOut, RuleIn, RuleOut

from .auth import display_name

router = APIRouter(prefix="/rules", tags=["规则"])


def _validate(kind: str, expr: dict, recover: dict) -> None:
    try:
        if kind == "state":
            compile_expr(expr["when"])
        elif kind == "threshold":
            compile_expr(expr["value"])
            if not expr.get("levels"):
                raise ExprError("至少配置一个阈值级别")
        else:
            raise ExprError(f"不支持的规则类型：{kind}")
        if recover.get("when"):
            compile_expr(recover["when"])
    except KeyError as e:
        raise BizError(ErrorCode.BAD_REQUEST, f"缺少字段：{e}") from e
    except ExprError as e:
        raise BizError(ErrorCode.BAD_REQUEST, str(e)) from e


@router.get("", response_model=ApiResponse[list[RuleOut]], summary="规则列表")
def list_rules(resource_type: str | None = None, db: Session = Depends(get_db),
               _: CurrentUser = Depends(require_perm("rule:view"))):
    q = select(AlarmRule).order_by(AlarmRule.id)
    if resource_type:
        q = q.where(AlarmRule.resource_type == resource_type)
    return ok([RuleOut.model_validate(r) for r in db.scalars(q)])


@router.get("/{rule_id}", response_model=ApiResponse[RuleOut], summary="规则详情")
def get_rule(rule_id: str, db: Session = Depends(get_db), _: CurrentUser = Depends(require_perm("rule:view"))):
    r = db.get(AlarmRule, rule_id)
    if r is None:
        raise not_found("规则")
    return ok(RuleOut.model_validate(r))


@router.put("/{rule_id}", response_model=ApiResponse[RuleOut], summary="修改规则（版本号 +1）")
def update_rule(rule_id: str, body: RuleIn, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                cu: CurrentUser = Depends(require_perm("rule:edit"))):
    r = db.get(AlarmRule, rule_id)
    if r is None:
        raise not_found("规则")
    _validate(body.kind, body.expr, body.recover)
    changed = {k: [getattr(r, k), v] for k, v in body.model_dump().items() if getattr(r, k) != v}
    for k, v in body.model_dump().items():
        setattr(r, k, v)
    r.version += 1
    r.updated_by = display_name(db, cu)
    audit.log("rule.update", target_type="rule", target_id=r.id, target_name=r.name,
              detail={"version": r.version, "changes": changed})
    return ok(RuleOut.model_validate(r))


@router.post("/dry-run", response_model=ApiResponse[DryRunOut], summary="规则试运行（不落库）")
def dry_run(body: DryRunIn, _: CurrentUser = Depends(require_perm("rule:view"))):
    _validate(body.kind, body.expr, body.recover)
    rule = Rule(id="DRY-RUN", name="试运行", resource_type="-", check_item="-", kind=body.kind, expr=body.expr,
                recover=body.recover)
    d = evaluate_rule(rule, body.facts, {}, None)
    recovered = False
    if d.match is None:
        try:
            recovered = rule.recovered(body.facts)
        except ExprError:
            recovered = False
    return ok(DryRunOut(matched=d.match is not None, severity=d.match.severity if d.match else None,
                        recovered=recovered, message=d.match.title if d.match else d.message))
