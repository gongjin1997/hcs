"""CCE 独立健康快照；不修改既有租户、任务或采集开关。"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table("cce_cluster_snapshot",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("project_ref", sa.Integer(), nullable=False),
        sa.Column("cluster_id", sa.String(255), nullable=False),
        sa.Column("cluster", json_type, nullable=False),
        sa.Column("nodes", json_type, nullable=False),
        sa.Column("nodepools", json_type, nullable=False),
        sa.Column("native", json_type, nullable=False),
        sa.Column("sources", json_type, nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("missing", sa.Boolean(), nullable=False))
    op.create_index("idx_cce_snapshot_scope", "cce_cluster_snapshot", ["tenant_id", "project_ref", "cluster_id"])


def downgrade() -> None:
    op.drop_table("cce_cluster_snapshot")
