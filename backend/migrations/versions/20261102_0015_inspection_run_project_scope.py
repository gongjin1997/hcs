"""Persist runtime project identity without inferring historical ownership."""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("inspection_run") as batch:
        batch.add_column(sa.Column("project_ref", sa.Integer(), nullable=True))
        batch.create_index("idx_inspection_run_scope_time", ["task_code", "tenant_id", "project_ref", "started_at", "id"])


def downgrade() -> None:
    with op.batch_alter_table("inspection_run") as batch:
        batch.drop_index("idx_inspection_run_scope_time")
        batch.drop_column("project_ref")
