from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from app.models import AlarmRule

from .model import Rule
from .recovery_evidence import upgrade_default

DEFAULT_RULES = Path(__file__).with_name("default_rules.yaml")


def load_yaml_rules(path: Path = DEFAULT_RULES) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def sync_default_rules(db: Session) -> int:
    """把内置规则写入数据库（已存在的规则不覆盖，保留页面上的调整；只给还没有处理建议的规则补上内置的处理建议）。"""
    added = 0
    for r in load_yaml_rules():
        row = db.get(AlarmRule, r["id"])
        if row is None:
            db.add(AlarmRule(**{"scope": {}, "recover": {}, "for_count": 1, **r}, updated_by="system"))
            added += 1
        else:
            upgrade_default(db, row)
            if not (row.runbook or "").strip() and r.get("runbook"):
                row.runbook = r["runbook"]
    db.commit()
    return added


def rules_by_target(db: Session, resource_types: set[str]) -> dict[tuple[str, str], list[Rule]]:
    out: dict[tuple[str, str], list[Rule]] = {}
    rows = db.query(AlarmRule).filter(AlarmRule.enabled.is_(True), AlarmRule.resource_type.in_(resource_types))
    for row in rows:
        out.setdefault((row.resource_type, row.check_item), []).append(Rule.from_obj(row))
    return out
