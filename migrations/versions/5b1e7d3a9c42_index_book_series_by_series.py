"""index book_series by series

Revision ID: 5b1e7d3a9c42
Revises: e7c2a94d1f58
Create Date: 2026-10-04

The region-key revision left every index on book_series leading with
book_asin: uq_book_series is (book_asin, book_region, series_asin,
series_region) and book_series_index was dropped behind it. Nothing led with
series_asin, so reading the books of a series filtered the whole table, and so
did the ON DELETE CASCADE from series, which looks up the child rows by
(series_asin, series_region) to delete them.

Measured on a scratch Postgres 16 holding 900k books, 150k series and 744k
book_series rows: the series-to-books read went from a parallel sequential
scan (89 ms) to an index probe (2.9 ms), and deleting one series with its
links from 170 ms to 2 ms. The build took 4.5 s.

Plain CREATE INDEX, not CONCURRENTLY: migrations/env.py runs the whole chain in
one transaction, and CONCURRENTLY cannot run inside one. The SHARE lock it
takes lasts the few seconds the build takes. Reads of book_series proceed;
writes to it queue behind the lock, including the cascades from deleting a
series or a book. The API runs this revision at startup while the seeder, the
backfill and the persist queue may be writing, and their connections carry a
30s statement_timeout, which the build fits inside.

lock_timeout is set first, as in e7c2a94d1f58, and is transaction-scoped so it
holds whatever revision the chain starts from. If a long-held lock on
book_series keeps the build from starting, the revision fails after 5s rather
than waiting, which would stall every writer queued behind its pending
request; the container restarts and retries. IF NOT EXISTS keeps a rerun
against a database that already has the index harmless.
"""
from typing import Sequence, Union

from alembic import op


revision: str = '5b1e7d3a9c42'
down_revision: Union[str, Sequence[str], None] = 'e7c2a94d1f58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_index(
        'book_series_series_index', 'book_series', ['series_asin', 'series_region'],
        unique=False, if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index('book_series_series_index', table_name='book_series', if_exists=True)
