"""add credit-order position to the author, narrator and genre links

Revision ID: 3c8e5a1f7d94
Revises: 5b1e7d3a9c42
Create Date: 2026-10-25

A book's authors, narrators and genres were served in whatever order the query
plan produced, which at scale is the unique index's order, not the order
Audible credits them in. position records the index in the credited list.

The column is nullable with no default, so Postgres adds it as a catalog change
only: no table rewrite and no scan, whatever the row count. ADD COLUMN still
takes ACCESS EXCLUSIVE on each table while it runs, which is milliseconds once
it has the lock, so lock_timeout is set first, as in 5b1e7d3a9c42. If a
long-held lock keeps it from starting, the revision fails after 5s rather than
queueing every writer behind it, and the container restarts and retries.
IF NOT EXISTS keeps a rerun harmless.

Existing links stay NULL and are served after positioned ones, in their old
order, until the book is next written.
"""
from typing import Sequence, Union

from alembic import op


revision: str = '3c8e5a1f7d94'
down_revision: Union[str, Sequence[str], None] = '5b1e7d3a9c42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ('author_book', 'book_narrator', 'book_genre')


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in TABLES:
        op.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "position" INTEGER')


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in TABLES:
        op.execute(f'ALTER TABLE {table} DROP COLUMN IF EXISTS "position"')
