"""
Alembic environment for the schema `libex_core.storage` creates.

This is the package's own chain, separate from the hosted app's: it versions
itself in `libex_core_alembic_version`, so the two never see each other's
revisions, and it describes only the tables in `CORE_TABLES`. The shared
metadata also carries the hosted app's own tables when the app is imported,
and those are none of this chain's business.

It never reads the process environment and never opens a database itself. The
caller (`libex_core.storage.upgrade`) hands it a connection through
`config.attributes["connection"]`, which is the only way it runs, and it runs
nothing offline: a migration that renders SQL for someone else to execute has
no connection to check the database it is aimed at.
"""

from alembic import context

import libex_core.storage.models  # noqa: F401 -- registers the tables on Base
from libex_core.storage.base import Base
from libex_core.storage.models import CORE_TABLES
from libex_core.storage.upgrade import VERSION_TABLE

def include_name(name, type_, parent_names):
    """Only the core tables, on the database side of a comparison."""
    if type_ == "table":
        return name in CORE_TABLES
    return True


def include_object(obj, name, type_, reflected, compare_to):
    """Only the core tables, on the metadata side of a comparison."""
    if type_ == "table":
        return name in CORE_TABLES
    return True


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is None:
        raise RuntimeError(
            "the libex_core migrations run on a connection handed in by "
            "libex_core.storage.upgrade, never on their own"
        )
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        version_table=VERSION_TABLE,
        include_name=include_name,
        include_object=include_object,
        compare_type=True,
        # SQLite's ALTER TABLE cannot do most of what a migration needs; batch
        # mode rebuilds the table instead. A no-op on Postgres.
        render_as_batch=True,
        # Alembic assumes SQLite cannot roll DDL back. It can, and the store
        # opens the transaction itself, so a failed migration leaves nothing.
        transactional_ddl=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("the libex_core migrations cannot render offline SQL")
run_migrations_online()
