"""add series_author pivot table

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f6
Create Date: 2026-04-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create series_author pivot table derived from book authorship."""
    op.create_table(
        'series_author',
        sa.Column('series_asin', sa.String(12), sa.ForeignKey('series.asin', ondelete='CASCADE'), nullable=False),
        sa.Column('author_id', sa.Integer, sa.ForeignKey('authors.id', ondelete='CASCADE'), nullable=False),
        sa.UniqueConstraint('series_asin', 'author_id', name='uq_series_author'),
    )
    op.create_index('series_author_index', 'series_author', ['series_asin', 'author_id'])
    op.create_index('author_series_index', 'series_author', ['author_id', 'series_asin'])

    # Backfill from existing book relationships — any author who wrote a book
    # in a series is considered an author of that series.
    op.execute("""
        INSERT INTO series_author (series_asin, author_id)
        SELECT DISTINCT bs.series_asin, ab.author_id
        FROM book_series bs
        JOIN author_book ab ON ab.book_asin = bs.book_asin
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    """Drop series_author pivot table."""
    op.drop_index('author_series_index', table_name='series_author')
    op.drop_index('series_author_index', table_name='series_author')
    op.drop_table('series_author')