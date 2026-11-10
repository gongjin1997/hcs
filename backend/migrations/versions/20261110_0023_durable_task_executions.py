"""巡检执行队列、租约与停止请求；保留原运行历史。"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    js = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table("task_execution",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("inspection_run.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("task_code", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Integer()), sa.Column("project_ref", sa.Integer()),
        sa.Column("request_id", sa.String(64), nullable=False), sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("scope_key", sa.String(64), nullable=False), sa.Column("active_scope", sa.String(64), unique=True),
        sa.Column("trigger_type", sa.String(16), nullable=False), sa.Column("requested_by", sa.String(128)),
        sa.Column("status", sa.String(16), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)), sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column("queue_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timeout_sec", sa.Integer(), nullable=False), sa.Column("owner_token", sa.String(64)),
        sa.Column("worker_id", sa.String(128)), sa.Column("params", js, nullable=False),
        sa.Column("target", js, nullable=False), sa.Column("progress", js, nullable=False),
        sa.Column("history", js, nullable=False), sa.Column("error_code", sa.String(64)),
        sa.UniqueConstraint("request_key", "scope_key", name="uq_task_execution_request_scope"))
    op.create_index("ix_task_execution_task_code", "task_execution", ["task_code"])
    op.create_index("ix_task_execution_request_id", "task_execution", ["request_id"])
    op.create_index("ix_task_execution_queue", "task_execution", ["status", "created_at"])
    op.create_index("ix_task_execution_scope_time", "task_execution", ["tenant_id", "project_ref", "created_at"])
    op.create_table("task_execution_cancel",
        sa.Column("execution_id", sa.String(36), sa.ForeignKey("task_execution.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False))


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM task_execution WHERE status IN ('QUEUED','RUNNING') LIMIT 1")).first():
        raise RuntimeError("仍有排队或运行中的巡检，先停止执行器并处理任务后再降级")
    op.drop_table("task_execution_cancel")
    op.drop_table("task_execution")
