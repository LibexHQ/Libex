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

There is deliberately no lock_timeout on the concurrent statements. They wait
for every transaction in the database that is older than they are, a backup
dump or a long refresh included, and a timeout would turn that wait into a
failed startup and a restart loop while protecting nothing, since the lock
they take blocks neither reads nor writes of books. So API startup waits for
the oldest open transaction to finish.

An interrupted CONCURRENTLY build leaves an INVALID index that IF NOT EXISTS
would take for a good one, so an invalid leftover is dropped before the build
and a rerun succeeds.
"""
import logging
import time
from typing import Sequence, Union

from alembic import context, op
from sqlalchemy import text


revision: str = 'c4f7a2e9d1b3'
down_revision: Union[str, Sequence[str], None] = 'b7e2d94c1a68'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NAME = 'ix_books_manual_entry'

logger = logging.getLogger('alembic.runtime.migration')


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # Offline there is no database to ask, so the drop is unconditional.
        if context.is_offline_mode() or op.get_bind().execute(
            text('SELECT 1 FROM pg_index WHERE indexrelid = to_regclass(:name) AND NOT indisvalid'),
            {'name': NAME},
        ).first():
            logger.warning('dropping the invalid leftover %s from an interrupted build', NAME)
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {NAME}')
        logger.info('building %s on books', NAME)
        started = time.monotonic()
        op.execute(
            f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {NAME} '
            'ON books (manual_entry_at DESC, asin, region) WHERE manual_entry_at IS NOT NULL'
        )
        logger.info('built %s in %.1fs', NAME, time.monotonic() - started)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {NAME}')
