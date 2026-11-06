from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import Auditor, get_auditor
from app.core.db import get_db
from app.core.deps import CurrentUser, require_perm
from app.core.response import ApiResponse, BizError, ErrorCode, not_found, ok
from app.models import AlarmRule
from app.models.base import utcnow
from app.rules.evaluator import evaluate_rule
from app.rules.expr import ExprError
from app.rules.model import Rule
from app.schemas.rule import DryRunIn, DryRunOut, RuleCreate, RuleIn, RuleOut, RuleTargetOut, RuleUpdate
from app.services.rule_service import list_rule_targets, validate_rule

from .auth import display_name

router = APIRouter(prefix="/rules", tags=["规则"])


@router.get("", response_model=ApiResponse[list[RuleOut]], summary="规则列表")
def list_rules(resource_type: str | None = None, db: Session = Depends(get_db),
               _: CurrentUser = Depends(require_perm("rule:view"))):
    q = select(AlarmRule).order_by(AlarmRule.id)
    if resource_type:
        q = q.where(AlarmRule.resource_type == resource_type)
    return ok([RuleOut.model_validate(r) for r in db.scalars(q)])


@router.get("/targets", response_model=ApiResponse[list[RuleTargetOut]], summary="实际支持的规则目标与事实字段")
def get_targets(_: CurrentUser = Depends(require_perm("rule:view"))):
    return ok(list_rule_targets())


@router.post("", response_model=ApiResponse[RuleOut], status_code=201, summary="创建自定义规则")
def create_rule(body: RuleCreate, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                cu: CurrentUser = Depends(require_perm("rule:edit"))):
    validate_rule(body, body.resource_type, body.check_item)
    if db.get(AlarmRule, body.id) is not None:
        raise BizError(ErrorCode.CONFLICT, "规则 ID 已存在", 409)
    rule = AlarmRule(**body.model_dump(), version=1, updated_by=display_name(db, cu))
    db.add(rule)
    try:
        db.flush()
        audit.log("rule.create", target_type="rule", target_id=rule.id, target_name=rule.name,
                  detail={"version": 1, "snapshot": RuleOut.model_validate(rule).model_dump(mode="json")}, commit=False)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise BizError(ErrorCode.CONFLICT, "规则 ID 已存在", 409) from exc
    return ok(RuleOut.model_validate(rule))


@router.get("/{rule_id}", response_model=ApiResponse[RuleOut], summary="规则详情")
def get_rule(rule_id: str, db: Session = Depends(get_db), _: CurrentUser = Depends(require_perm("rule:view"))):
    r = db.get(AlarmRule, rule_id)
    if r is None:
        raise not_found("规则")
    return ok(RuleOut.model_validate(r))


@router.put("/{rule_id}", response_model=ApiResponse[RuleOut], summary="修改规则（版本号 +1）")
def update_rule(rule_id: str, body: RuleUpdate, db: Session = Depends(get_db), audit: Auditor = Depends(get_auditor),
                cu: CurrentUser = Depends(require_perm("rule:edit"))):
    r = db.get(AlarmRule, rule_id)
    if r is None:
        raise not_found("规则")
    validate_rule(body, r.resource_type, r.check_item, allow_unavailable=not body.enabled)
    if body.version is not None and body.version != r.version:
        raise BizError(ErrorCode.CONFLICT, "规则版本已变化，请刷新后重试", 409)
    values = body.model_dump(exclude={"version"})
    before = RuleOut.model_validate(r).model_dump(mode="json")
    changed = {k: [getattr(r, k), v] for k, v in values.items() if getattr(r, k) != v}
    updated = db.execute(update(AlarmRule).where(AlarmRule.id == rule_id, AlarmRule.version == r.version)
                         .values(**values, version=r.version + 1, updated_by=display_name(db, cu), updated_at=utcnow())
                         .returning(AlarmRule.id)).scalar_one_or_none()
    if updated is None:
        db.rollback()
        raise BizError(ErrorCode.CONFLICT, "规则版本已变化，请刷新后重试", 409)
    db.refresh(r)
    audit.log("rule.update", target_type="rule", target_id=r.id, target_name=r.name,
              detail={"version": r.version, "changes": changed, "before": before,
                      "snapshot": RuleOut.model_validate(r).model_dump(mode="json")})
    return ok(RuleOut.model_validate(r))


@router.post("/dry-run", response_model=ApiResponse[DryRunOut], summary="规则试运行（不落库）")
def dry_run(body: DryRunIn, _: CurrentUser = Depends(require_perm("rule:view"))):
    validate_rule(RuleIn(name="试运行", kind=body.kind, expr=body.expr, recover=body.recover),
                  body.resource_type, body.check_item)
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
