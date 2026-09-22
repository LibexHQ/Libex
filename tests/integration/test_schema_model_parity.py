"""
The migrated schema and the ORM's own metadata describe the same columns.

Every column exists in two places that nothing mechanically keeps together:
a migration writes the DDL, and app/db/models.py declares the mapped
attribute every reader and writer in the codebase goes through. Adding one
without the other is silent in both directions, and both directions have
real cost:

  column in the database, absent from the model
      Nothing reads it and nothing writes it. It holds whatever the
      migration's default gave it, forever, while the model looks complete.
      chapters_checked_at shipped in exactly this state.

  attribute on the model, absent from the database
      Every statement touching the table fails at runtime with an
      UndefinedColumn from Postgres -- loud, but only once the code is
      deployed, and by then the migration that should have carried it is
      already merged.

Neither is visible to anything the suite runs today.
tests/test_startup_migrations.py asserts that startup issues no alembic
upgrade; it says nothing about what the migrations produce. The backup
verifier (app/services/backup/dump.py) compares Base.metadata.tables against
the artifact at TABLE granularity, so a table present on both sides with a
column missing from one passes it cleanly.

These run at no cost beyond what the harness already pays: the integration
conftest starts one Postgres 16 container and runs `alembic upgrade head`
against it at import time, so what is reflected here is a schema that is
already built. No second database, no second upgrade, no DDL of their own.

Types are compared as the PostgreSQL dialect renders them, not as Python
objects -- Text and TEXT, Double and DOUBLE PRECISION, and an enum and its
type name are the same column declared from two directions, and comparing
the type instances themselves would report all three as differences.
"""

# Third party
import pytest
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

# Local
from app.db.base import Base


_DIALECT = postgresql.dialect()

# One test per table rather than one over all of them, so a failure names
# the table in its own node id instead of burying it in a diff of every
# column in the schema.
_MAPPED_TABLES = sorted(Base.metadata.tables)


def _rendered_type(column_type) -> str:
    """The column's type as PostgreSQL itself spells it."""
    return str(column_type.compile(dialect=_DIALECT))


async def _reflected_columns(session, table_name):
    """Columns the migrated database actually holds, keyed by name."""
    connection = await session.connection()
    columns = await connection.run_sync(
        lambda sync_connection: inspect(sync_connection).get_columns(table_name)
    )
    return {column["name"]: column for column in columns}


# ============================================================
# EVERY MAPPED TABLE WAS MIGRATED
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_mapped_table_exists_in_the_migrated_schema(db_session):
    """The table-granularity check, kept here so the column checks below
    cannot fail for the uninteresting reason that the table is missing."""
    connection = await db_session.connection()
    present = await connection.run_sync(
        lambda sync_connection: set(inspect(sync_connection).get_table_names())
    )

    missing = sorted(set(_MAPPED_TABLES) - present)
    assert missing == [], (
        f"{missing} are declared on Base.metadata but no migration creates them."
    )


# ============================================================
# COLUMN NAMES MATCH, BOTH DIRECTIONS
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("table_name", _MAPPED_TABLES)
async def test_mapped_columns_and_migrated_columns_are_the_same_set(db_session, table_name):
    """An identity, not a subset in either direction.

    Asserted as a set rather than a count for the same reason the response
    shape is: a count agrees by coincidence whenever one column is added and
    another removed in the same change, which is exactly what a rename is.
    """
    reflected = await _reflected_columns(db_session, table_name)
    mapped = {column.name for column in Base.metadata.tables[table_name].columns}

    only_on_the_model = sorted(mapped - set(reflected))
    only_in_the_database = sorted(set(reflected) - mapped)

    assert only_on_the_model == [], (
        f"{table_name}: {only_on_the_model} are mapped in app/db/models.py but "
        "no migration creates them. Every statement touching this table will "
        "fail against a migrated database."
    )
    assert only_in_the_database == [], (
        f"{table_name}: {only_in_the_database} exist in the migrated schema but "
        "are not mapped in app/db/models.py. Nothing reads or writes them."
    )


# ============================================================
# TYPE AND NULLABILITY MATCH
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("table_name", _MAPPED_TABLES)
async def test_mapped_column_types_and_nullability_match_the_migration(db_session, table_name):
    """The half a name-only comparison misses.

    A column migrated as TEXT and mapped as Integer, or migrated nullable
    and mapped NOT NULL, passes every name check there is and then either
    stores something the model cannot read back or rejects a write the model
    believes is legal. Both are reported together so one run names every
    disagreement in the table rather than the first one.
    """
    reflected = await _reflected_columns(db_session, table_name)
    disagreements = []

    for column in Base.metadata.tables[table_name].columns:
        actual = reflected.get(column.name)
        if actual is None:
            continue  # Reported by the set test above; not restated here.

        mapped_type = _rendered_type(column.type)
        actual_type = _rendered_type(actual["type"])
        if mapped_type != actual_type:
            disagreements.append(
                f"{column.name}: model says {mapped_type}, database says {actual_type}"
            )

        if bool(column.nullable) != bool(actual["nullable"]):
            disagreements.append(
                f"{column.name}: model says "
                f"{'NULL' if column.nullable else 'NOT NULL'}, database says "
                f"{'NULL' if actual['nullable'] else 'NOT NULL'}"
            )

    assert disagreements == [], f"{table_name}: " + "; ".join(disagreements)
