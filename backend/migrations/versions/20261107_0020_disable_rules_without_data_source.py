"""停用没有数据来源的内置规则：它们每轮都“缺采”，实例因此长期显示“部分覆盖”，无法判为健康。

- R-DCS-007（连接数使用率）：DCS v2 实例列表 / 统计接口没有实例级连接数与上限（API §4.1.2、§4.2.6）。
- R-GS-007 / R-GS-008（CPU / 内存使用率）：GaussDB 原生接口没有实时 CPU / 内存，只有磁盘用量与巡检记录。
- 只停用未经人工修改、且仍处于启用状态的系统规则；修改前的状态备份在 sys_setting（rule-nosource-0020:*），
  downgrade 时在规则未被再次修改的前提下还原。
"""
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

RULES = ("R-DCS-007", "R-GS-007", "R-GS-008")
MARKER = "system:no-data-source-v1"
PREFIX = "rule-nosource-0020:"
SYSTEM_EDITORS = ("system", "system:evidence-recovery-v1")


def _tables() -> tuple[sa.TableClause, sa.TableClause]:
    rule = sa.table("alarm_rule", sa.column("id", sa.String()), sa.column("enabled", sa.Boolean()),
                    sa.column("version", sa.Integer()), sa.column("updated_by", sa.String()),
                    sa.column("updated_at", sa.DateTime(timezone=True)))
    setting = sa.table("sys_setting", sa.column("key", sa.String()), sa.column("value", sa.JSON()),
                       sa.column("updated_by", sa.String()), sa.column("updated_at", sa.DateTime(timezone=True)))
    return rule, setting


def upgrade() -> None:
    connection = op.get_bind()
    rule, setting = _tables()
    now = datetime.now(UTC)
    for code in RULES:
        row = connection.execute(sa.select(rule).where(rule.c.id == code)).mappings().first()
        if row is None or row["updated_by"] not in SYSTEM_EDITORS or not row["enabled"]:
            continue  # 已被人工修改或已停用的规则不动
        key = PREFIX + code
        if connection.execute(sa.select(setting.c.key).where(setting.c.key == key)).first() is not None:
            continue
        before = {"enabled": True, "version": row["version"], "updated_by": row["updated_by"],
                  "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None}
        connection.execute(setting.insert().values(key=key, value={"before": before}, updated_by=MARKER,
                                                   updated_at=now))
        connection.execute(rule.update().where(rule.c.id == code).values(
            enabled=False, version=row["version"] + 1, updated_by=MARKER, updated_at=now))


def downgrade() -> None:
    connection = op.get_bind()
    rule, setting = _tables()
    for code in RULES:
        key = PREFIX + code
        backup = connection.execute(sa.select(setting.c.value).where(setting.c.key == key)).scalar_one_or_none()
        if backup is None:
            continue
        row = connection.execute(sa.select(rule).where(rule.c.id == code)).mappings().first()
        before = dict(backup["before"])
        if row is not None and row["updated_by"] == MARKER and row["version"] == before["version"] + 1:
            at = before.pop("updated_at", None)
            connection.execute(rule.update().where(rule.c.id == code).values(
                enabled=True, version=before["version"], updated_by=before["updated_by"],
                updated_at=datetime.fromisoformat(at) if at else datetime.now(UTC)))
        connection.execute(setting.delete().where(setting.c.key == key))
