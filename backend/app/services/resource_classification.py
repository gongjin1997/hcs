"""独立资源分类映射：有序、忽略大小写的字面包含匹配，不回写业务或云资源类型。"""
import logging
from string import ascii_lowercase, ascii_uppercase
from typing import cast

from pydantic import ValidationError
from sqlalchemy import LargeBinary, and_, case, func, literal, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models import Resource, SysSetting
from app.schemas.resource import ClassificationConfig, ClassificationRule

KEY = "resource_classification"
log = logging.getLogger(__name__)


def defaults() -> ClassificationConfig:
    return ClassificationConfig(rules=[ClassificationRule(
        id=f"ecs-os-{category.lower()}", resource_type="ecs_server", field="os", pattern=pattern, category=category)
        for pattern, category in (("MRS", "MRS"), ("CCE", "CCE"), ("his", "HIS"))], fallback="其他")


def load(db: Session) -> ClassificationConfig:
    row = db.scalar(select(SysSetting).where(SysSetting.key == KEY).execution_options(populate_existing=True))
    if row is None:
        return defaults()
    try:
        return ClassificationConfig.model_validate(row.value)
    except ValidationError:
        log.warning("resource_classification_invalid_config_using_defaults")
        return defaults()


def save(db: Session, config: ClassificationConfig, operator: str) -> dict:
    before = load(db).model_dump(mode="json")
    value = config.model_dump(mode="json")
    row = db.get(SysSetting, KEY)
    if row is None:
        db.add(SysSetting(key=KEY, value=value, updated_by=operator))
    else:
        row.value, row.updated_by = value, operator
    db.flush()
    return {key: [before[key], value[key]] for key in value if before[key] != value[key]}


def classify(resource: Resource, config: ClassificationConfig) -> tuple[str, str | None]:
    for rule in config.rules:
        if not rule.enabled or rule.resource_type != resource.resource_type:
            continue
        value = (resource.attrs or {}).get("os") if rule.field == "os" else getattr(resource, rule.field)
        if isinstance(value, str) and rule.pattern.casefold() in value.casefold():
            return rule.category, rule.id
    return config.fallback, None


def _column(db: Session, field: str) -> ColumnElement[str]:
    if field != "os":
        return cast(ColumnElement[str], getattr(Resource, field))
    # 非字符串的历史 JSON 值不应被数据库隐式转换后误匹配。
    is_text = (func.jsonb_typeof(Resource.attrs["os"]) == "string" if db.get_bind().dialect.name == "postgresql"
               else func.json_type(Resource.attrs, "$.os") == "text")
    return case((is_text, Resource.attrs["os"].as_string()), else_="")


def expression(db: Session, config: ClassificationConfig, *, tenant_ids: list[int] | None = None,
               resource_types: list[str] | None = None) -> ColumnElement[str]:
    """SQL CASE 用于分页前筛选 / 分组；对非 ASCII 值补齐 Python casefold 的数据库差异。"""
    rules = [rule for rule in config.rules if rule.enabled and
             (resource_types is None or rule.resource_type in resource_types)]
    unicode_values: dict[tuple[str, str], list[str]] = {}
    conditions: list[tuple[ColumnElement[bool], str]] = []
    postgres = db.get_bind().dialect.name == "postgresql"
    for rule in rules:
        column = _column(db, rule.field)
        key = (rule.resource_type, rule.field)
        if key not in unicode_values:
            byte_length = func.octet_length(column) if postgres else func.length(sql_cast(column, LargeBinary))
            query = select(column).where(Resource.resource_type == rule.resource_type,
                                          byte_length > func.length(column)).distinct()
            if tenant_ids is not None:
                query = query.where(Resource.tenant_id.in_(tenant_ids))
            unicode_values[key] = [value for value in db.scalars(query) if isinstance(value, str)]
        # ASCII 转换不依赖 PostgreSQL 的本地化大小写规则；% / _ / 转义符均按字面匹配。
        folded = func.translate(column, ascii_uppercase, ascii_lowercase) if postgres else func.lower(column)
        matches = folded.contains(rule.pattern.casefold(), autoescape=True)
        non_ascii = unicode_values[key]
        if non_ascii:
            exact = [value for value in non_ascii if rule.pattern.casefold() in value.casefold()]
            matches = or_(and_(column.not_in(non_ascii), matches), column.in_(exact))
        conditions.append((and_(Resource.resource_type == rule.resource_type, matches), rule.category))
    return case(*conditions, else_=config.fallback) if conditions else literal(config.fallback)


def summary(db: Session, config: ClassificationConfig, *, tenant_ids: list[int] | None = None,
            resource_types: list[str] | None = None) -> dict:
    categories = list(dict.fromkeys([rule.category for rule in config.rules if rule.enabled and
                                    (resource_types is None or rule.resource_type in resource_types)] + [config.fallback]))
    category = expression(db, config, tenant_ids=tenant_ids, resource_types=resource_types).label("classification")
    query = select(category, func.count()).where(Resource.lifecycle == "ACTIVE")
    if tenant_ids is not None:
        query = query.where(Resource.tenant_id.in_(tenant_ids))
    if resource_types is not None:
        query = query.where(Resource.resource_type.in_(resource_types))
    counts = dict.fromkeys(categories, 0)
    counts.update(dict(db.execute(query.group_by(category)).all()))
    return {"categories": categories, "counts": counts, "total": sum(counts.values())}
