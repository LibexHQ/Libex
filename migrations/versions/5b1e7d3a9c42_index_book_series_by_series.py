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
takes blocks writes to book_series, not reads, for those few seconds, and the
revision runs with writers stopped. IF NOT EXISTS keeps a rerun against a
database that already has the index harmless.
"""
from typing import Sequence, Union

from alembic import op


revision: str = '5b1e7d3a9c42'
down_revision: Union[str, Sequence[str], None] = 'e7c2a94d1f58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'book_series_series_index', 'book_series', ['series_asin', 'series_region'],
        unique=False, if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index('book_series_series_index', table_name='book_series', if_exists=True)
