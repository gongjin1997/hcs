from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.collectors.scope import MAX_PATTERN_LEN, MAX_PATTERNS

Patterns = Annotated[list[Annotated[str, Field(max_length=MAX_PATTERN_LEN)]], Field(max_length=MAX_PATTERNS)]


class ScopeRuleIn(BaseModel):
    """一条范围规则。同一规则内各维度取“且”，同一维度内多个值取“或”；值可含 * ? 通配，区分大小写。"""

    model_config = ConfigDict(extra="forbid")
    effect: Literal["include", "exclude"] = Field(description="include = 只采集命中的对象；exclude = 屏蔽命中的对象")
    match: dict[str, Patterns] = Field(min_length=1, max_length=16)
    note: str | None = Field(None, max_length=200, description="备注，例如为什么屏蔽")


class ScopeIn(BaseModel):
    """rules 为 null：租户改为跟随全局（全局范围不允许）；[]：不限制（全部采集）。必须显式给出。"""

    model_config = ConfigDict(extra="forbid")
    rules: list[ScopeRuleIn] | None       # 条数上限由 collect_scope.normalize_rules 校验（MAX_RULES）
