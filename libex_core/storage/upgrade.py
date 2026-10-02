"""
Running the package's own migration chain, and reading where a database is in
it.

The chain lives in `libex_core/storage/migrations/` and versions itself in its
own table, so it never meets the hosted app's. Alembic is a heavy import and
only an upgrade or a status check needs it, so it is imported inside the
functions that use it and importing this module loads none of it.

Everything here is synchronous and takes a SQLAlchemy `Connection`; the store
runs it on an async connection through `run_sync`.
"""

# Standard library
from dataclasses import dataclass
from importlib import resources

# Third party
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

VERSION_TABLE = "libex_core_alembic_version"

# Where a database stands against the chain.
EMPTY = "empty"          # no tables at all: never initialised
CURRENT = "current"      # at the head this library ships
BEHIND = "behind"        # at an older revision this library knows: upgrade
AHEAD = "ahead"          # at a revision this library has never heard of
FOREIGN = "foreign"      # tables present, no revision recorded: not ours

# Taken as a transaction-scoped lock on Postgres so two processes upgrading
# the same fresh database do not both create the tables ("LIBX" in ASCII).
_PG_LOCK_KEY = 0x4C494258


@dataclass(frozen=True)
class SchemaState:
    state: str
    revision: str | None


def _config(connection: Connection):
    from alembic.config import Config

    config = Config()
    location = resources.files("libex_core.storage").joinpath("migrations")
    # ConfigParser interpolation reads a percent sign as syntax.
    config.set_main_option("script_location", str(location).replace("%", "%%"))
    config.attributes["connection"] = connection
    return config


def _script(connection: Connection):
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_config(connection))


def head_revision(connection: Connection) -> str:
    """The revision this library's chain ends at."""
    head = _script(connection).get_current_head()
    if head is None:
        raise RuntimeError("the libex_core migration chain is empty")
    return head


def _stored_revision(connection: Connection, tables: set[str]) -> str | None:
    if VERSION_TABLE not in tables:
        return None
    rows = connection.execute(text(f"SELECT version_num FROM {VERSION_TABLE}")).all()
    if len(rows) > 1:
        raise RuntimeError("the migration version table records more than one revision")
    return rows[0][0] if rows else None


def read_state(connection: Connection) -> SchemaState:
    """Classifies the database. Reads only; never creates anything."""
    tables = set(inspect(connection).get_table_names())
    revision = _stored_revision(connection, tables)
    if revision is None:
        others = {t for t in tables if t != VERSION_TABLE and not t.startswith("sqlite_")}
        return SchemaState(FOREIGN if others else EMPTY, None)
    script = _script(connection)
    if revision == script.get_current_head():
        return SchemaState(CURRENT, revision)
    known = script.get_revision(revision) if _is_known(script, revision) else None
    return SchemaState(BEHIND if known is not None else AHEAD, revision)


def _is_known(script, revision: str) -> bool:
    from alembic.util.exc import CommandError

    try:
        return script.get_revision(revision) is not None
    except CommandError:
        return False


def upgrade_to_head(connection: Connection) -> None:
    """Runs every pending revision on `connection`, inside the transaction it
    is already in; the caller commits."""
    from alembic import command

    if connection.dialect.name == "postgresql":
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _PG_LOCK_KEY})
    command.upgrade(_config(connection), "head")
