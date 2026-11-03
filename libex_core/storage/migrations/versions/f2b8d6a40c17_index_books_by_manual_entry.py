"""index the hand-entered books for the delisted list

Revision ID: f2b8d6a40c17
Revises: d3a8f1b65e90
Create Date: 2026-10-28

The partial index the hosted chain's revision of the same change builds, over
the same columns and predicate.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'f2b8d6a40c17'
down_revision: Union[str, Sequence[str], None] = 'd3a8f1b65e90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'ix_books_manual_entry',
        'books',
        [sa.text('manual_entry_at DESC'), 'asin', 'region'],
        postgresql_where=sa.text('manual_entry_at IS NOT NULL'),
        sqlite_where=sa.text('manual_entry_at IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('ix_books_manual_entry', table_name='books')
