"""租户独立 ManageOne 配置：旧租户默认空配置，不启用、不选择指标来源。"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hcs_account", sa.Column("manageone_config",
        sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    # SQLite and PostgreSQL both support DROP COLUMN directly; batch recreation
    # would DROP the parent table while tenant/project foreign keys are enabled.
    op.drop_column("hcs_account", "manageone_config")
