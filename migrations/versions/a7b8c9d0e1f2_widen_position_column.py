"""widen book_series position column

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-05-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('book_series', 'position',
                     type_=sa.String(100),
                     existing_type=sa.String(20),
                     existing_nullable=True)


def downgrade() -> None:
    op.alter_column('book_series', 'position',
                     type_=sa.String(20),
                     existing_type=sa.String(100),
                     existing_nullable=True)