"""index book_series by series

Revision ID: 9d2f6b4e8a17
Revises: a4d91f7c3b26
Create Date: 2026-10-04

Every index on book_series leads with book_asin, so reading the books of a
series, and the ON DELETE CASCADE from series, filter the whole table. This
adds the index that leads with the series, matching the hosted chain's
revision of the same change.
"""
from typing import Sequence, Union

from alembic import op


revision: str = '9d2f6b4e8a17'
down_revision: Union[str, Sequence[str], None] = 'a4d91f7c3b26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'book_series_series_index', 'book_series', ['series_asin', 'series_region'],
        unique=False, if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index('book_series_series_index', table_name='book_series', if_exists=True)
