"""add parent_id to catalog_genres

Revision ID: c4e6a8b0d2f3
Revises: b2d4f6a8c0e1
Create Date: 2026-06-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c4e6a8b0d2f3"
down_revision = "b2d4f6a8c0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent guard: skip if the parent_id column already exists (e.g. the
    # table was created out-of-band with the new shape before this runs).
    conn = op.get_bind()
    exists = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'catalog_genres' AND column_name = 'parent_id'"
        )
    ).scalar()
    if exists:
        return

    # catalog_genres is a repopulated cache of Audible's category taxonomy, so
    # existing rows don't need a real parent — they get the parent sentinel ''
    # and are overwritten on the next taxonomy refresh. Add the column with a
    # server_default so the not-null backfill is automatic, then drop the
    # default (the app always supplies the value going forward).
    op.add_column(
        "catalog_genres",
        sa.Column("parent_id", sa.String(20), nullable=False, server_default=""),
    )
    op.alter_column("catalog_genres", "parent_id", server_default=None)

    # Move from PK (region, genre_id) to (region, genre_id, parent_id) so a leaf
    # that appears under two parents is stored once per parent rather than
    # colliding. Parents carry parent_id = '' (top-level); leaves carry their
    # parent's id.
    op.drop_constraint("catalog_genres_pkey", "catalog_genres", type_="primary")
    op.create_primary_key(
        "catalog_genres_pkey",
        "catalog_genres",
        ["region", "genre_id", "parent_id"],
    )


def downgrade() -> None:
    # Collapse back to (region, genre_id). Dual-parent leaves would now collide,
    # so de-duplicate to one row per (region, genre_id) before restoring the PK.
    op.execute(
        """
        DELETE FROM catalog_genres a
        USING catalog_genres b
        WHERE a.region = b.region
          AND a.genre_id = b.genre_id
          AND a.parent_id > b.parent_id
        """
    )
    op.drop_constraint("catalog_genres_pkey", "catalog_genres", type_="primary")
    op.create_primary_key(
        "catalog_genres_pkey",
        "catalog_genres",
        ["region", "genre_id"],
    )
    op.drop_column("catalog_genres", "parent_id")