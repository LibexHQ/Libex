"""add credit-order position to the author, narrator and genre links

Revision ID: 6e1b4c9a2d57
Revises: 9d2f6b4e8a17
Create Date: 2026-10-25

A book's authors, narrators and genres were served in whatever order the query
plan produced, not the order Audible credits them in. position records the
index in the credited list. Nullable with no default, so existing links stay
NULL, are served after positioned ones, and take a position the next time the
book is written. Matches the hosted chain's revision of the same change.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '6e1b4c9a2d57'
down_revision: Union[str, Sequence[str], None] = '9d2f6b4e8a17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ('author_book', 'book_narrator', 'book_genre')


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column('position', sa.Integer(), nullable=True))


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, 'position')
