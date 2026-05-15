"""expand narrators with profile fields

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-05-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('narrators', sa.Column('description', sa.Text(), nullable=True))
    op.add_column('narrators', sa.Column('image', sa.Text(), nullable=True))
    op.add_column('narrators', sa.Column('website', sa.Text(), nullable=True))
    op.add_column('narrators', sa.Column('wikipedia_url', sa.Text(), nullable=True))
    op.add_column('narrators', sa.Column('fetched_description', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('narrators', 'fetched_description')
    op.drop_column('narrators', 'wikipedia_url')
    op.drop_column('narrators', 'website')
    op.drop_column('narrators', 'image')
    op.drop_column('narrators', 'description')