"""用户会话版本与持久化的单会话撤销记录。"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sys_user") as batch:
        batch.add_column(sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))
    op.create_table("revoked_token", sa.Column("jti", sa.String(64), primary_key=True),
                    sa.Column("user_id", sa.Integer(), nullable=False),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_revoked_token_user_id", "revoked_token", ["user_id"])
    op.create_index("ix_revoked_token_expires_at", "revoked_token", ["expires_at"])


def downgrade() -> None:
    op.drop_table("revoked_token")
    with op.batch_alter_table("sys_user") as batch:
        batch.drop_column("token_version")
