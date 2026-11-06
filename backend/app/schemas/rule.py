from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints, model_validator

from .common import ORMModel
from .guide import GuideField

ShortText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=128)]
Expression = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)] | StrictInt
Severity = Literal["P1", "P2", "P3", "P4"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RuleLevelIn(_StrictModel):
    severity: Severity
    op: Literal[">=", ">", "<=", "<"] = ">="
    threshold: Number


class StateExprIn(_StrictModel):
    when: Expression
    severity: Severity
    critical_severity: Severity | None = None
    value_field: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    threshold_text: Annotated[str, StringConstraints(max_length=256)] | None = None
    title: Annotated[str, StringConstraints(max_length=500)] | None = None


class ThresholdExprIn(_StrictModel):
    value: Expression
    levels: Annotated[list[RuleLevelIn], Field(min_length=1, max_length=4)]
    unit: Annotated[str, StringConstraints(max_length=32)] = ""
    title: Annotated[str, StringConstraints(max_length=500)] | None = None

    @model_validator(mode="after")
    def unique_severity(self) -> Self:
        if len({level.severity for level in self.levels}) != len(self.levels):
            raise ValueError("阈值级别不能重复")
        return self


class RecoverIn(_StrictModel):
    for_count: Annotated[StrictInt, Field(ge=1, le=1000)] | None = None
    when: Expression | None = None
    below: Number | None = None


class RuleOut(ORMModel):
    id: str
    name: str
    resource_type: str
    check_item: str
    kind: str
    scope: dict
    expr: dict
    for_count: int
    recover: dict
    repeat_interval_sec: int | None = None
    runbook: str | None = None
    enabled: bool
    version: int
    updated_by: str | None = None
    updated_at: datetime | None = None


class RuleIn(_StrictModel):
    name: ShortText
    kind: Literal["state", "threshold"]
    scope: dict = Field(default_factory=dict)
    expr: dict
    for_count: Annotated[StrictInt, Field(ge=1, le=1000)] = 1
    recover: dict = Field(default_factory=dict)
    repeat_interval_sec: Annotated[StrictInt, Field(ge=0, le=31536000)] | None = None
    runbook: Annotated[str, StringConstraints(max_length=10000)] | None = None
    enabled: StrictBool = True

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        schema = StateExprIn if self.kind == "state" else ThresholdExprIn
        self.expr = schema.model_validate(self.expr).model_dump(exclude_none=True)
        recover = RecoverIn.model_validate(self.recover)
        if self.kind == "state" and recover.below is not None:
            raise ValueError("状态规则不支持恢复阈值 below")
        if recover.when is not None and recover.below is not None:
            raise ValueError("恢复条件 when 与 below 不能同时设置")
        self.recover = recover.model_dump(exclude_none=True)
        if self.scope:
            raise ValueError("当前规则评估器不支持 scope 筛选，请使用空对象")
        return self


class RuleCreate(RuleIn):
    id: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
    resource_type: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    check_item: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class RuleUpdate(RuleIn):
    version: Annotated[StrictInt, Field(ge=1)] | None = None


class RuleTargetOut(BaseModel):
    resource_type: str
    name: str
    service: str
    check_item: str
    collection_tasks: list[str]
    facts: list[GuideField]
    expression_examples: list[dict]
    limitations: list[str]
    collection_available: bool = True


class DryRunIn(_StrictModel):
    kind: Literal["state", "threshold"]
    expr: dict
    recover: dict = Field(default_factory=dict)
    facts: dict
    resource_type: str | None = None
    check_item: str | None = None

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        definition = RuleIn(name="试运行", kind=self.kind, expr=self.expr, recover=self.recover)
        self.expr, self.recover = definition.expr, definition.recover
        if (self.resource_type is None) != (self.check_item is None):
            raise ValueError("resource_type 与 check_item 必须同时提供")
        return self


class DryRunOut(BaseModel):
    matched: bool
    severity: str | None = None
    recovered: bool
    message: str
