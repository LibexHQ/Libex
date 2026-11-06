"""trigram indexes for the /db search filters

Revision ID: a8c3e51f7d29
Revises: c4f7a2e9d1b3
Create Date: 2026-11-04

The /db filters match with ILIKE '%term%', which no btree can serve, so every
search is a sequential scan of the whole table. A GIN index over gin_trgm_ops
serves that exact predicate unchanged: the queries and their results stay the
same, only the plan changes.

Indexed here: books.title, subtitle, publisher and isbn, authors.name,
series.title and narrators.name. The long text columns (description, summary,
copyright) are deliberately left out: their indexes cost the most disk and build
time, and are a separate call.
Not indexed: genres.name, which is a few thousand rows, and the narrator
attribute columns (gender, source, cultural_heritage), which are low
cardinality: a trigram index over a handful of distinct values selects nothing
and only costs writes.

The extension is created in the public schema and the operator class is
qualified with it, so the build does not depend on search_path. If pg_trgm
already lives in another schema, CREATE EXTENSION IF NOT EXISTS would pass
silently and the qualified class would then fail to resolve, so that case
raises up front instead.

Each index is built CONCURRENTLY in an autocommit block, for the reasons
c4f7a2e9d1b3 gives: the earlier revisions in an upgrade share one transaction
and a plain CREATE INDEX would keep readers of books waiting through the build.
There is deliberately no lock_timeout, since the concurrent statements wait for
every older transaction and a timeout would only turn that wait into a restart
loop. An interrupted build leaves an INVALID index that IF NOT EXISTS would take
for a good one, so an invalid leftover is dropped before each build.

Downgrade drops the indexes and keeps the extension: other objects may come to
depend on it, and dropping it needs ownership that the role running migrations
does not necessarily hold.

Applying the same DDL out of band ahead of the deploy is safe; the build here
then finds each index valid and leaves it alone.
"""
import logging
import time
from typing import Sequence, Union

from alembic import context, op
from sqlalchemy import text


revision: str = 'a8c3e51f7d29'
down_revision: Union[str, Sequence[str], None] = 'c4f7a2e9d1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index, table, column)
INDEXES = (
    ('ix_books_title_trgm', 'books', 'title'),
    ('ix_books_subtitle_trgm', 'books', 'subtitle'),
    ('ix_books_publisher_trgm', 'books', 'publisher'),
    ('ix_books_isbn_trgm', 'books', 'isbn'),
    ('ix_authors_name_trgm', 'authors', 'name'),
    ('ix_series_title_trgm', 'series', 'title'),
    ('ix_narrators_name_trgm', 'narrators', 'name'),
)

logger = logging.getLogger('alembic.runtime.migration')


def upgrade() -> None:
    offline = context.is_offline_mode()
    if not offline:
        schema = op.get_bind().execute(text(
            "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
            "WHERE e.extname = 'pg_trgm'"
        )).scalar()
        if schema not in (None, 'public'):
            raise RuntimeError(
                f'pg_trgm is installed in the {schema} schema; these indexes use public.gin_trgm_ops'
            )
    with op.get_context().autocommit_block():
        op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public')
        for name, table, column in INDEXES:
            # Offline there is no database to ask, so the drop is unconditional.
            if offline or op.get_bind().execute(
                text('SELECT 1 FROM pg_index WHERE indexrelid = to_regclass(:name) AND NOT indisvalid'),
                {'name': name},
            ).first():
                if not offline:
                    logger.warning('dropping the invalid leftover %s from an interrupted build', name)
                op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {name}')
            if not offline:
                logger.info('building %s on %s', name, table)
            started = time.monotonic()
            op.execute(
                f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} '
                f'ON {table} USING gin ({column} public.gin_trgm_ops)'
            )
            if not offline:
                logger.info('built %s in %.1fs', name, time.monotonic() - started)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _table, _column in INDEXES:
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {name}')
