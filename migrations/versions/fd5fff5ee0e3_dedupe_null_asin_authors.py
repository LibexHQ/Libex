"""dedupe null-asin authors and add a partial unique index

Revision ID: fd5fff5ee0e3
Revises: c4e6a8b0d2f3
Create Date: 2026-07-04

The authors_asin_region_name_unique constraint doesn't cover null asins —
Postgres treats NULLs as distinct in unique indexes — so two books by the same
not-yet-ASINed author being persisted concurrently could each insert a null-asin
row for the same (name, region). Once two exist, the writer's null-asin lookup
found more than one row and the book write failed.

This migration cleans up any existing null-asin duplicates and adds a partial
unique index so they can't form again.

For each (name, region) that has more than one null-asin row, the lowest id is
kept as the survivor (matching the writer, which now takes the oldest row). The
other rows' pivots (author_book, author_genre, series_author) are repointed onto
the survivor first — this ordering matters because those pivots have
ON DELETE CASCADE on author_id, so deleting a loser row before repointing would
take its book links with it. Books the survivor already has are skipped, since
the pivots are uniquely constrained; the cascade then removes those leftover
loser rows when the loser author is deleted.

Every statement is scoped to asin IS NULL and only to (name, region) groups with
more than one row, so single null-asin rows and every ASINed row are untouched.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'fd5fff5ee0e3'
down_revision: Union[str, Sequence[str], None] = 'c4e6a8b0d2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Dedupe null-asin authors (repoint pivots, drop losers) then add the index."""

    # Repoint author_book from each loser onto its survivor (lowest id in the
    # group), skipping books the survivor already has (uq_author_book).
    op.execute("""
        UPDATE author_book ab
        SET author_id = keep.keep_id
        FROM (
            SELECT a.id AS loser_id, m.keep_id
            FROM authors a
            JOIN (
                SELECT name, region, MIN(id) AS keep_id
                FROM authors
                WHERE asin IS NULL
                GROUP BY name, region
                HAVING COUNT(*) > 1
            ) m ON a.name = m.name AND a.region = m.region
            WHERE a.asin IS NULL AND a.id <> m.keep_id
        ) keep
        WHERE ab.author_id = keep.loser_id
          AND NOT EXISTS (
              SELECT 1 FROM author_book existing
              WHERE existing.author_id = keep.keep_id
                AND existing.book_asin = ab.book_asin
          )
    """)

    # Repoint author_genre the same way (uq_author_genre on author_id, genre_asin).
    op.execute("""
        UPDATE author_genre ag
        SET author_id = keep.keep_id
        FROM (
            SELECT a.id AS loser_id, m.keep_id
            FROM authors a
            JOIN (
                SELECT name, region, MIN(id) AS keep_id
                FROM authors
                WHERE asin IS NULL
                GROUP BY name, region
                HAVING COUNT(*) > 1
            ) m ON a.name = m.name AND a.region = m.region
            WHERE a.asin IS NULL AND a.id <> m.keep_id
        ) keep
        WHERE ag.author_id = keep.loser_id
          AND NOT EXISTS (
              SELECT 1 FROM author_genre existing
              WHERE existing.author_id = keep.keep_id
                AND existing.genre_asin = ag.genre_asin
          )
    """)

    # Repoint series_author the same way (uq_series_author on series_asin, author_id).
    op.execute("""
        UPDATE series_author sa
        SET author_id = keep.keep_id
        FROM (
            SELECT a.id AS loser_id, m.keep_id
            FROM authors a
            JOIN (
                SELECT name, region, MIN(id) AS keep_id
                FROM authors
                WHERE asin IS NULL
                GROUP BY name, region
                HAVING COUNT(*) > 1
            ) m ON a.name = m.name AND a.region = m.region
            WHERE a.asin IS NULL AND a.id <> m.keep_id
        ) keep
        WHERE sa.author_id = keep.loser_id
          AND NOT EXISTS (
              SELECT 1 FROM series_author existing
              WHERE existing.series_asin = sa.series_asin
                AND existing.author_id = keep.keep_id
          )
    """)

    # Delete the loser author rows. Their pivots are now either repointed or, for
    # books the survivor already had, still pointing at the loser — the author_id
    # cascade removes those leftover duplicate pivot rows on delete.
    op.execute("""
        DELETE FROM authors a
        USING (
            SELECT name, region, MIN(id) AS keep_id
            FROM authors
            WHERE asin IS NULL
            GROUP BY name, region
            HAVING COUNT(*) > 1
        ) m
        WHERE a.name = m.name
          AND a.region = m.region
          AND a.asin IS NULL
          AND a.id <> m.keep_id
    """)

    # Prevent recurrence: at most one null-asin row per (name, region).
    op.execute("""
        CREATE UNIQUE INDEX uq_authors_name_region_null_asin
        ON authors (name, region)
        WHERE asin IS NULL
    """)


def downgrade() -> None:
    """
    Drop the partial unique index.

    The row dedupe is not reversible — the deleted duplicate rows and their
    redundant pivot links can't be restored (they were redundant copies of the
    surviving rows). This matches a1b2c3d4e5f6, which likewise can't restore the
    duplicate pivot rows it removed.
    """
    op.execute("DROP INDEX IF EXISTS uq_authors_name_region_null_asin")