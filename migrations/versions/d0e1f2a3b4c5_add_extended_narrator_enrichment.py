"""add extended narrator enrichment columns

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-05-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, Sequence[str], None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('narrators', sa.Column('genres_narrated', postgresql.JSONB(), nullable=True))
    op.add_column('narrators', sa.Column('audiobooks_produced', sa.String(50), nullable=True))
    op.add_column('narrators', sa.Column('cultural_heritage', sa.Text(), nullable=True))
    op.add_column('narrators', sa.Column('publishers', postgresql.JSONB(), nullable=True))
    op.add_column('narrators', sa.Column('social_links', postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('narrators', 'social_links')
    op.drop_column('narrators', 'publishers')
    op.drop_column('narrators', 'cultural_heritage')
    op.drop_column('narrators', 'audiobooks_produced')
    op.drop_column('narrators', 'genres_narrated')