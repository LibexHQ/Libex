"""add unique constraints to pivot tables

Revision ID: a1b2c3d4e5f6
Revises: 22195f2af627
Create Date: 2026-04-18

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '22195f2af627'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add unique constraints to pivot tables to prevent duplicate rows."""

    # Remove duplicate rows before adding constraints — keep one of each pair
    op.execute("""
        DELETE FROM author_book a
        USING author_book b
        WHERE a.ctid < b.ctid
        AND a.author_id = b.author_id
        AND a.book_asin = b.book_asin
    """)

    op.execute("""
        DELETE FROM book_series a
        USING book_series b
        WHERE a.ctid < b.ctid
        AND a.book_asin = b.book_asin
        AND a.series_asin = b.series_asin
    """)

    op.execute("""
        DELETE FROM author_genre a
        USING author_genre b
        WHERE a.ctid < b.ctid
        AND a.author_id = b.author_id
        AND a.genre_asin = b.genre_asin
    """)

    op.execute("""
        DELETE FROM book_genre a
        USING book_genre b
        WHERE a.ctid < b.ctid
        AND a.book_asin = b.book_asin
        AND a.genre_asin = b.genre_asin
    """)

    op.execute("""
        DELETE FROM book_narrator a
        USING book_narrator b
        WHERE a.ctid < b.ctid
        AND a.book_asin = b.book_asin
        AND a.narrator_name = b.narrator_name
    """)

    # Add unique constraints
    op.create_unique_constraint(
        'uq_author_book', 'author_book', ['author_id', 'book_asin']
    )
    op.create_unique_constraint(
        'uq_book_series', 'book_series', ['book_asin', 'series_asin']
    )
    op.create_unique_constraint(
        'uq_author_genre', 'author_genre', ['author_id', 'genre_asin']
    )
    op.create_unique_constraint(
        'uq_book_genre', 'book_genre', ['book_asin', 'genre_asin']
    )
    op.create_unique_constraint(
        'uq_book_narrator', 'book_narrator', ['book_asin', 'narrator_name']
    )


def downgrade() -> None:
    """Remove unique constraints from pivot tables."""
    op.drop_constraint('uq_author_book', 'author_book')
    op.drop_constraint('uq_book_series', 'book_series')
    op.drop_constraint('uq_author_genre', 'author_genre')
    op.drop_constraint('uq_book_genre', 'book_genre')
    op.drop_constraint('uq_book_narrator', 'book_narrator')