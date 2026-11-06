"""DCS V2 原生实际采样；不从历史快照或 AOM 回填。"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metrics = ("memory_usage", "used_memory_mb", "max_memory_mb", "used_cpu_seconds", "input_kbps", "output_kbps")
    counts = ("keys", "cmd_get_count", "cmd_set_count")
    op.create_table("dcs_metric_sample",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("resource_ref", sa.Integer(), sa.ForeignKey("resource.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
        sa.Column("project_ref", sa.Integer(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("api_version", sa.String(8), nullable=False),
        sa.Column("endpoint", sa.String(32), nullable=False),
        *(sa.Column(key, sa.Float(), nullable=True) for key in metrics),
        *(sa.Column(key, sa.BigInteger(), nullable=True) for key in counts),
        sa.CheckConstraint("api_version = 'v2' AND source = 'DCS'", name="ck_dcs_metric_source"),
        *(sa.CheckConstraint(f"{key} IS NULL OR {key} >= 0", name=f"ck_dcs_metric_{key}")
          for key in (*metrics, *counts) if key != "max_memory_mb"),
        sa.CheckConstraint("max_memory_mb IS NULL OR max_memory_mb > 0", name="ck_dcs_metric_max_memory_mb"))
    op.create_index("uk_dcs_metric_resource_time", "dcs_metric_sample", ["resource_ref", "observed_at"], unique=True)
    op.create_index("idx_dcs_metric_scope_time", "dcs_metric_sample", ["tenant_id", "project_ref", "observed_at"])
    op.create_index("idx_dcs_metric_observed_at", "dcs_metric_sample", ["observed_at"])


def downgrade() -> None:
    op.drop_table("dcs_metric_sample")
