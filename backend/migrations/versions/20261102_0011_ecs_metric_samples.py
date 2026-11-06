"""ECS 指标按真实观测时间保存；拆分指标任务，保留既有启停选择。"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("ecs_metric_sample",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("resource_ref", sa.Integer(), sa.ForeignKey("resource.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
        sa.Column("project_ref", sa.Integer(), nullable=True),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("unit", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_sec", sa.Integer(), nullable=False),
        sa.Column("dimensions", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.CheckConstraint("value >= 0 AND value <= 100", name="ck_ecs_metric_percent"),
        sa.CheckConstraint("metric IN ('cpu_util', 'mem_util', 'disk_util')", name="ck_ecs_metric_name"),
        sa.CheckConstraint("period_sec >= 0", name="ck_ecs_metric_period"))
    op.create_index("uk_ecs_metric_sample", "ecs_metric_sample",
                    ["resource_ref", "metric", "source", "observed_at"], unique=True)
    op.create_index("idx_ecs_metric_scope_time", "ecs_metric_sample", ["tenant_id", "project_ref", "observed_at"])
    op.create_index("idx_ecs_metric_observed_at", "ecs_metric_sample", ["observed_at"])
    task = sa.Table("inspection_task", sa.MetaData(), autoload_with=op.get_bind())
    old = op.get_bind().execute(sa.select(task).where(task.c.code == "ecs_status")).mappings().first()
    exists = op.get_bind().scalar(sa.select(task.c.code).where(task.c.code == "ecs_metrics"))
    setting = sa.Table("sys_setting", sa.MetaData(), autoload_with=op.get_bind())
    snapshot = op.get_bind().scalar(sa.select(setting.c.value).where(setting.c.key == "ecs_metrics_task_downgrade"))
    if exists is None and isinstance(snapshot, dict) and isinstance(snapshot.get("enabled"), bool):
        interval = snapshot.get("interval_sec")
        if isinstance(interval, int) and not isinstance(interval, bool) and interval >= 60:
            op.get_bind().execute(task.insert().values(code="ecs_metrics", name="ECS 使用指标采样", service="ecs",
                interval_sec=interval, cron=snapshot.get("cron"), enabled=snapshot["enabled"],
                params=snapshot.get("params") if isinstance(snapshot.get("params"), dict) else {}))
            return
    if old and exists is None:
        interval = old["interval_sec"] if old["interval_sec"] != 120 else 300
        op.get_bind().execute(task.insert().values(code="ecs_metrics", name="ECS 使用指标采样", service="ecs",
            interval_sec=max(60, interval), cron=old["cron"], enabled=old["enabled"], params=old["params"] or {}))


def downgrade() -> None:
    # 只删除本次新增的任务；原有状态任务和人员配置均保留。
    task = sa.Table("inspection_task", sa.MetaData(), autoload_with=op.get_bind())
    setting = sa.Table("sys_setting", sa.MetaData(), autoload_with=op.get_bind())
    current = op.get_bind().execute(sa.select(task).where(task.c.code == "ecs_metrics")).mappings().first()
    if current:
        value = {key: current[key] for key in ("interval_sec", "cron", "enabled", "params")}
        key = "ecs_metrics_task_downgrade"
        if op.get_bind().scalar(sa.select(setting.c.key).where(setting.c.key == key)) is None:
            from datetime import UTC, datetime

            op.get_bind().execute(setting.insert().values(key=key, value=value, updated_by="migration",
                                                        updated_at=datetime.now(UTC)))
        else:
            op.get_bind().execute(setting.update().where(setting.c.key == key).values(value=value))
    op.get_bind().execute(task.delete().where(task.c.code == "ecs_metrics"))
    op.drop_table("ecs_metric_sample")
