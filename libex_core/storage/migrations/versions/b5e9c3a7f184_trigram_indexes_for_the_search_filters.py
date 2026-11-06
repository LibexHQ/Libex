"""trigram indexes for the search filters

Revision ID: b5e9c3a7f184
Revises: f2b8d6a40c17
Create Date: 2026-11-04

The GIN gin_trgm_ops indexes the hosted chain's a8c3e51f7d29 builds, over the
same columns and under the same names (the chains must build one schema), so
ILIKE '%term%' filters use an index instead of scanning the table. Postgres only: SQLite has no operator class for
it and the revision does nothing there.

This chain runs in one transaction under an advisory lock, so the build cannot
be CONCURRENTLY. Each CREATE INDEX takes a SHARE lock on its table for the
length of the build: reads carry on, writes to that table wait.

The operator class lives in the schema pg_trgm is installed in, so that is read
from the catalog and used to qualify it. If the extension is missing it is
created in public, but only when the role can: the extension must be available
to the server and the role needs CREATE on the database and on public. The
check is made first and raises rather than trying and catching, so a role that
cannot create it fails here with the command to run, and the transaction rolls
back with no revision stamped.

genres.name and the narrator attribute columns are skipped: few rows, or few
distinct values, so a trigram index would select nothing and only cost writes.

Downgrade drops the indexes and keeps the extension, which other objects may
depend on and which dropping needs ownership of.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = 'b5e9c3a7f184'
down_revision: Union[str, Sequence[str], None] = 'f2b8d6a40c17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index, table, column)
INDEXES = (
    ('ix_books_title_trgm', 'books', 'title'),
    ('ix_books_subtitle_trgm', 'books', 'subtitle'),
    ('ix_books_publisher_trgm', 'books', 'publisher'),
    ('ix_books_isbn_trgm', 'books', 'isbn'),
    ('ix_authors_name_trgm', 'authors', 'name'),
    ('ix_series_title_trgm', 'series', 'title'),
    ('ix_narrators_name_trgm', 'narrators', 'name'),
)

CREATE_COMMAND = 'CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public'


def _trigram_schema(bind) -> str:
    """The schema pg_trgm is installed in, quoted, creating it in public first
    if it is missing and the role is allowed to."""
    installed = bind.execute(text(
        "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
        "WHERE e.extname = 'pg_trgm'"
    )).scalar()
    if installed is None:
        can_create = bind.execute(text(
            "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm') "
            "AND has_database_privilege(current_user, current_database(), 'CREATE') "
            "AND has_schema_privilege(current_user, 'public', 'CREATE')"
        )).scalar()
        if not can_create:
            raise RuntimeError(
                'the search indexes need the pg_trgm extension, which is not installed and '
                'cannot be created by this role; a privileged role must run: ' + CREATE_COMMAND
            )
        op.execute(CREATE_COMMAND)
        installed = 'public'
    return bind.dialect.identifier_preparer.quote(installed)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    opclass = f'{_trigram_schema(bind)}.gin_trgm_ops'
    for name, table, column in INDEXES:
        op.create_index(name, table, [column], postgresql_using='gin', postgresql_ops={column: opclass})


def downgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return
    for name, table, _column in INDEXES:
        op.drop_index(name, table_name=table)
