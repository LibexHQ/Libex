"""add last_seeded_at to authors series narrators

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-05-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, Sequence[str], None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('authors', sa.Column('last_seeded_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('series', sa.Column('last_seeded_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('narrators', sa.Column('last_seeded_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('narrators', 'last_seeded_at')
    op.drop_column('series', 'last_seeded_at')
    op.drop_column('authors', 'last_seeded_at')