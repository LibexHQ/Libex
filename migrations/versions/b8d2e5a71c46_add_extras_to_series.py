"""add audible_extras and extras_withheld to series

Revision ID: b8d2e5a71c46
Revises: a3f1c0d84b27
Create Date: 2026-10-01

A series profile now carries every product key beyond asin, title and
publisher_summary, as audibleExtras with extrasWithheld beside it, exactly as
a book does. Without columns for them the writer dropped both and every
store-served response, the outage fallback included, answered without them.

Both are nullable JSONB with no default, the same shape and meaning as the
books columns added in a3f1c0d84b27: NULL says no response has written the
row since the columns landed. The series table is small, so no storage
parameters are set on it, unlike books.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b8d2e5a71c46'
down_revision: Union[str, Sequence[str], None] = 'a3f1c0d84b27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.add_column('series', sa.Column('audible_extras', postgresql.JSONB(), nullable=True))
    op.add_column('series', sa.Column('extras_withheld', postgresql.JSONB(), nullable=True))
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '3s'")
    op.drop_column('series', 'extras_withheld')
    op.drop_column('series', 'audible_extras')
    op.execute("RESET lock_timeout")
