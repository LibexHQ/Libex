"""add manual_entry_at to books

Revision ID: d3a8f1b65e90
Revises: 6e1b4c9a2d57
Create Date: 2026-10-25

Marks a book that started as a manual entry. Set on insert, never updated,
never cleared. Nullable with no default, so existing rows stay NULL. Matches
the hosted chain's revision of the same change.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'd3a8f1b65e90'
down_revision: Union[str, Sequence[str], None] = '6e1b4c9a2d57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('books') as batch_op:
        batch_op.add_column(sa.Column('manual_entry_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('books') as batch_op:
        batch_op.drop_column('manual_entry_at')
