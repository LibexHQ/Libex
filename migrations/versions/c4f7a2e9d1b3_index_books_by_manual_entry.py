"""index the hand-entered books for the delisted list

Revision ID: c4f7a2e9d1b3
Revises: b7e2d94c1a68
Create Date: 2026-10-28

A partial index on books (manual_entry_at DESC, asin, region) WHERE
manual_entry_at IS NOT NULL, which is the order and the predicate of the
delisted list. Without it every page is a sequential scan of the whole table.

Built under lock_timeout, not CONCURRENTLY. manual_entry_at was added by the
previous revision and is NULL on every row, so the index holds no entries and
the build is one pass over the heap with nothing to sort or write. That pass
blocks writers to books for its duration, which is the cost; the revision runs
in the entrypoint before the app serves, so nothing but a background persist
waits on it. CONCURRENTLY would instead make two passes, wait on every
transaction open when it starts, need its own autocommit block outside the
revision's transaction, and on interruption leave an INVALID index that
IF NOT EXISTS then treats as present. lock_timeout is set first, as in
b7e2d94c1a68: if a long-held lock keeps it from starting, the revision fails
after 5s rather than queueing every writer behind it, and the container
restarts and retries.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'c4f7a2e9d1b3'
down_revision: Union[str, Sequence[str], None] = 'b7e2d94c1a68'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        'CREATE INDEX IF NOT EXISTS ix_books_manual_entry '
        'ON books (manual_entry_at DESC, asin, region) WHERE manual_entry_at IS NOT NULL'
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute('DROP INDEX IF EXISTS ix_books_manual_entry')
