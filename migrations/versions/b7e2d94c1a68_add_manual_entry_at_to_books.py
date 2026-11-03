"""add manual_entry_at to books

Revision ID: b7e2d94c1a68
Revises: 3c8e5a1f7d94
Create Date: 2026-10-25

Marks a book that started as a manual entry. Set on insert, never updated,
never cleared.

The column is nullable with no default, so Postgres adds it as a catalog change
only: no table rewrite and no scan. ADD COLUMN still takes ACCESS EXCLUSIVE on
books while it runs, so lock_timeout is set first, as in 3c8e5a1f7d94. If a
long-held lock keeps it from starting, the revision fails after 5s rather than
queueing every writer behind it, and the container restarts and retries.
IF NOT EXISTS keeps a rerun harmless. Existing rows stay NULL.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'b7e2d94c1a68'
down_revision: Union[str, Sequence[str], None] = '3c8e5a1f7d94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute('ALTER TABLE books ADD COLUMN IF NOT EXISTS manual_entry_at TIMESTAMP WITH TIME ZONE')


def downgrade() -> None:
    op.execute('ALTER TABLE books DROP COLUMN IF EXISTS manual_entry_at')
