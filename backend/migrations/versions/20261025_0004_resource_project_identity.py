"""资源身份改为项目内唯一，保留无项目历史记录的独立唯一约束。"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    constraints = sa.inspect(connection).get_unique_constraints("resource")
    naming = {"uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s"}
    with op.batch_alter_table("resource", naming_convention=naming) as batch:
        for constraint in constraints:
            if set(constraint["column_names"]) == {"resource_type", "resource_id"}:
                batch.drop_constraint(constraint["name"] or "uq_resource_resource_type_resource_id", type_="unique")
    op.create_index("uk_resource_project", "resource", ["project_ref", "resource_type", "resource_id"], unique=True,
                    sqlite_where=sa.text("project_ref IS NOT NULL"), postgresql_where=sa.text("project_ref IS NOT NULL"))
    op.create_index("uk_resource_legacy", "resource", ["resource_type", "resource_id"], unique=True,
                    sqlite_where=sa.text("project_ref IS NULL"), postgresql_where=sa.text("project_ref IS NULL"))
    op.create_index("idx_resource_project_type", "resource", ["project_ref", "resource_type"])


def downgrade() -> None:
    duplicate = op.get_bind().execute(sa.text(
        "SELECT resource_type, resource_id FROM resource GROUP BY resource_type, resource_id HAVING count(*) > 1 LIMIT 1"
    )).first()
    if duplicate:
        raise RuntimeError("无法降级：存在跨项目同 ID 资源。请先备份并处理重复资源及其关联记录，再重试降级。")
    op.drop_index("idx_resource_project_type", table_name="resource")
    op.drop_index("uk_resource_legacy", table_name="resource")
    op.drop_index("uk_resource_project", table_name="resource")
    with op.batch_alter_table("resource") as batch:
        batch.create_unique_constraint("uq_resource_resource_type_resource_id", ["resource_type", "resource_id"])
