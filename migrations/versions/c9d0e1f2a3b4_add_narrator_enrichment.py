"""add enrichment columns to narrators

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-05-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, Sequence[str], None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('narrators', sa.Column('languages', postgresql.JSONB(), nullable=True))
    op.add_column('narrators', sa.Column('accents', postgresql.JSONB(), nullable=True))
    op.add_column('narrators', sa.Column('gender', sa.String(20), nullable=True))
    op.add_column('narrators', sa.Column('source', sa.Text(), nullable=True))
    op.add_column('narrators', sa.Column('source_url', sa.Text(), nullable=True))
    op.add_column('narrators', sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('narrators', 'source_updated_at')
    op.drop_column('narrators', 'source_url')
    op.drop_column('narrators', 'source')
    op.drop_column('narrators', 'gender')
    op.drop_column('narrators', 'accents')
    op.drop_column('narrators', 'languages')