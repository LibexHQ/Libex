"""add region-scoped stats indexes

Revision ID: 1203df663bc3
Revises: 82b62dfaed93
Create Date: 2026-08-26

Region-scoped stats counts forced a heap seq scan without these: a bare
books(region) still seq-scans the booksWithChapters join because that also
needs books.asin, so books(region, asin) is covering and makes it index-only
once the visibility map is current for the scanned pages -- pre-VACUUM (a
freshly loaded or write-heavy table) Postgres can't trust the visibility map
and falls back to a Bitmap Heap Scan instead, still indexed but at roughly
2.6x the buffers. series(region) covers the series count the same way.
authors needs nothing — authors_region_name_index already leads with region.
No partial index for seriesRegionUnknown (region IS NULL) — a btree already
indexes NULLs.

Plain CREATE INDEX, not CONCURRENTLY: migrations/env.py wraps every migration
in a single transaction, and CONCURRENTLY cannot run inside one. The SHARE
lock this takes blocks writes, not reads, for the seconds it takes to build
(~36MB/~4.9s for books, ~1MB/~90ms for series).
"""
from typing import Sequence, Union

from alembic import op


revision: str = '1203df663bc3'
down_revision: Union[str, Sequence[str], None] = '82b62dfaed93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('books_region_asin_index', 'books', ['region', 'asin'], unique=False)
    op.create_index('series_region_index', 'series', ['region'], unique=False)


def downgrade() -> None:
    op.drop_index('series_region_index', table_name='series')
    op.drop_index('books_region_asin_index', table_name='books')
