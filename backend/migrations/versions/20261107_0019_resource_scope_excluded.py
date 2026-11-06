"""采集范围：resource.scope_excluded（资源被范围规则排除的物化标记）。

范围规则本身存放在 sys_setting（键 collect_scope:{租户}:{资源类型}），不需要建表。
降级只删除该列，规则保留；再次升级后下一次巡检或保存规则会重新计算标记。
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resource", sa.Column("scope_excluded", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("resource") as batch:
        batch.drop_column("scope_excluded")
