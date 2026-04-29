"""add is_vvab column to books

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add is_vvab boolean column to books."""
    op.add_column(
        'books',
        sa.Column('is_vvab', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column('books', 'is_vvab', server_default=None)


def downgrade() -> None:
    """Drop is_vvab column from books."""
    op.drop_column('books', 'is_vvab')
