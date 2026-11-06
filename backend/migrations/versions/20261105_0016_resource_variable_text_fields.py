"""Store unbounded upstream AZ/IP values and composed resource specifications."""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_LEGACY_WIDTHS = {"az": 64, "ip": 128, "spec": 128}


def upgrade() -> None:
    with op.batch_alter_table("resource") as batch:
        for column in _LEGACY_WIDTHS:
            batch.alter_column(column, existing_type=sa.String(length=_LEGACY_WIDTHS[column]),
                               type_=sa.Text(), existing_nullable=True)


def downgrade() -> None:
    connection = op.get_bind()
    for column, width in _LEGACY_WIDTHS.items():
        overflow = connection.execute(sa.text(
            f"SELECT 1 FROM resource WHERE {column} IS NOT NULL AND length({column}) > :width LIMIT 1"),
            {"width": width}).first()
        if overflow:
            raise RuntimeError(f"无法降级：resource.{column} 存在超过 {width} 字符的值，拒绝截断")
    with op.batch_alter_table("resource") as batch:
        for column, width in _LEGACY_WIDTHS.items():
            batch.alter_column(column, existing_type=sa.Text(), type_=sa.String(length=width), existing_nullable=True)
