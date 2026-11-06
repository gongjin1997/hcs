"""保存实际采集的 DCS 内存历史；不回填历史，本地样本固定保留 90 天。"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dcs_memory_sample",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("resource_ref", sa.Integer(), sa.ForeignKey("resource.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
        sa.Column("project_ref", sa.Integer(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("memory_usage", sa.Float(), nullable=False),
        sa.Column("used_memory_mb", sa.Float(), nullable=True),
        sa.Column("max_memory_mb", sa.Float(), nullable=True),
        sa.CheckConstraint("memory_usage >= 0", name="ck_dcs_memory_usage"),
        sa.CheckConstraint("used_memory_mb IS NULL OR used_memory_mb >= 0", name="ck_dcs_memory_used"),
        sa.CheckConstraint("max_memory_mb IS NULL OR max_memory_mb > 0", name="ck_dcs_memory_max"),
    )
    op.create_index("uk_dcs_memory_resource_time", "dcs_memory_sample", ["resource_ref", "observed_at"], unique=True)
    op.create_index("idx_dcs_memory_observed_at", "dcs_memory_sample", ["observed_at"])


def downgrade() -> None:
    op.drop_index("idx_dcs_memory_observed_at", table_name="dcs_memory_sample")
    op.drop_index("uk_dcs_memory_resource_time", table_name="dcs_memory_sample")
    op.drop_table("dcs_memory_sample")
