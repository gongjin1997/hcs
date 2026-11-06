"""GaussDB 原生存储本地真实定时采样。"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("gaussdb_storage_sample",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("resource_ref", sa.Integer(), sa.ForeignKey("resource.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("project_ref", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("used_gb", sa.Float(), nullable=True),
        sa.Column("total_gb", sa.Float(), nullable=True),
        sa.Column("usage_percent", sa.Float(), nullable=True),
        sa.CheckConstraint("used_gb IS NULL OR used_gb >= 0", name="ck_gaussdb_storage_used"),
        sa.CheckConstraint("total_gb IS NULL OR total_gb >= 0", name="ck_gaussdb_storage_total"),
        sa.CheckConstraint("usage_percent IS NULL OR usage_percent >= 0", name="ck_gaussdb_storage_usage"),
        sa.CheckConstraint("status IN ('AVAILABLE', 'PARTIAL', 'ERROR', 'UNAVAILABLE')", name="ck_gaussdb_storage_status"))
    op.create_index("uk_gaussdb_storage_resource_time", "gaussdb_storage_sample", ["resource_ref", "observed_at"], unique=True)
    op.create_index("idx_gaussdb_storage_scope_time", "gaussdb_storage_sample",
                    ["tenant_id", "project_ref", "resource_ref", "observed_at"])
    op.create_index("idx_gaussdb_storage_observed_at", "gaussdb_storage_sample", ["observed_at"])


def downgrade() -> None:
    op.drop_table("gaussdb_storage_sample")
