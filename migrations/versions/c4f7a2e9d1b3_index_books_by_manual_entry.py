"""index the hand-entered books for the delisted list

Revision ID: c4f7a2e9d1b3
Revises: b7e2d94c1a68
Create Date: 2026-10-28

A partial index on books (manual_entry_at DESC, asin, region) WHERE
manual_entry_at IS NOT NULL, which is the order and the predicate of the
delisted list. Without it every page is a sequential scan of the whole table.

Built CONCURRENTLY, in an autocommit block. The revisions in one upgrade share
a single transaction, so the previous revision's ALTER TABLE books holds its
ACCESS EXCLUSIVE lock until this one finishes, and a plain CREATE INDEX would
keep readers of books waiting through the whole build. Entering the block
commits that transaction first, which releases the lock.

An interrupted CONCURRENTLY build leaves an INVALID index that IF NOT EXISTS
would take for a good one, so an invalid leftover is dropped before the build
and a rerun succeeds. lock_timeout is a plain SET here, because SET LOCAL does
nothing outside a transaction, and is reset afterwards.
"""
from typing import Sequence, Union

from alembic import context, op
from sqlalchemy import text


revision: str = 'c4f7a2e9d1b3'
down_revision: Union[str, Sequence[str], None] = 'b7e2d94c1a68'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NAME = 'ix_books_manual_entry'


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        try:
            # Offline there is no database to ask, so the drop is unconditional.
            if context.is_offline_mode() or op.get_bind().execute(
                text(
                    'SELECT 1 FROM pg_index WHERE indexrelid = to_regclass(:name) '
                    'AND NOT indisvalid'
                ),
                {'name': NAME},
            ).first():
                op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {NAME}')
            op.execute(
                f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {NAME} '
                'ON books (manual_entry_at DESC, asin, region) WHERE manual_entry_at IS NOT NULL'
            )
        finally:
            op.execute('RESET lock_timeout')


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        try:
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {NAME}')
        finally:
            op.execute('RESET lock_timeout')
