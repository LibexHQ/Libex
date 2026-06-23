"""add catalog genres

Revision ID: b2d4f6a8c0e1
Revises: e1f2a3b4c5d6
Create Date: 2026-06-22 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b2d4f6a8c0e1"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent guard: skip if the table already exists (e.g. created
    # out-of-band before this migration runs).
    conn = op.get_bind()
    exists = conn.execute(
        sa.text("SELECT 1 FROM pg_tables WHERE tablename = 'catalog_genres'")
    ).scalar()
    if exists:
        return

    op.create_table(
        "catalog_genres",
        sa.Column("region", sa.String(2), nullable=False),
        sa.Column("genre_id", sa.String(20), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("last_checked", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("region", "genre_id"),
    )


def downgrade() -> None:
    op.drop_table("catalog_genres")