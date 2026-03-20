"""Create cache table

Revision ID: 9203c248b749
Revises:
Create Date: 2026-03-19
"""

# Standard library
from typing import Sequence, Union

# Third party
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = '9203c248b749'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'cache',
        sa.Column('key', sa.String(500), primary_key=True),
        sa.Column('value', JSONB, nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_cache_expires_at', 'cache', ['expires_at'])


def downgrade() -> None:
    op.drop_index('ix_cache_expires_at', table_name='cache')
    op.drop_table('cache')