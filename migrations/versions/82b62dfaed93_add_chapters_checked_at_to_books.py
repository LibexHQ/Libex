"""add chapters_checked_at to books

Revision ID: 82b62dfaed93
Revises: fd5fff5ee0e3
Create Date: 2026-07-29

Marks whether a book has been checked for chapter data. NULL means never
checked; a timestamp means we've asked Audible for its chapters (and either
stored them in tracks, or found Audible has none). Used by the chapter
backfill so it can tell "not yet processed" from "processed, no chapters" and
drain its work queue without re-fetching books that have no chapters.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '82b62dfaed93'
down_revision: Union[str, Sequence[str], None] = 'fd5fff5ee0e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "books",
        sa.Column("chapters_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("books", "chapters_checked_at")