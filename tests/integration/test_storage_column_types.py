"""
The libex_core storage column types, against real Postgres.

SQLite is covered in tests/libex_core/test_storage_schema.py. These pin what
the hosted backend does with the same types: a plain date still binds to a
timestamptz column, and a bare Python None into a JSONB column is the JSON
value 'null' while an explicit none_as_null bind is SQL NULL -- the difference
the JSONDocument comment documents.
"""

# Standard library
from datetime import date, datetime, timedelta, timezone

# Third party
import pytest
from sqlalchemy import insert, select, text, type_coerce
from sqlalchemy.dialects.postgresql import JSONB

# Local
from libex_core.storage.models import Book


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_date_bound_to_a_utc_column_stores_and_reads_back(db_session):
    await db_session.execute(insert(Book).values(
        asin="B000PGDATE", title="t", region="us", release_date=date(2024, 3, 1)))

    value = (await db_session.execute(select(Book.release_date))).scalar_one()

    assert value.utcoffset() == timedelta(0)
    assert (value.year, value.month, value.day) == (2024, 3, 1)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_offset_datetime_round_trips_as_the_same_utc_instant(db_session):
    written = datetime(2024, 3, 1, 12, 0, tzinfo=timezone(timedelta(hours=5)))
    await db_session.execute(insert(Book).values(
        asin="B000PGTZ01", title="t", region="us", release_date=written))

    value = (await db_session.execute(select(Book.release_date))).scalar_one()

    assert value == written
    assert value.utcoffset() == timedelta(0)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_bare_none_in_a_jsonb_column_is_json_null_not_sql_null(db_session):
    await db_session.execute(insert(Book).values(
        asin="B000PGNUL1", title="t", region="us", plans=None))

    row = (await db_session.execute(text(
        "select plans is null, jsonb_typeof(plans) from books"))).one()

    assert row == (False, "null")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_explicit_none_as_null_bind_is_sql_null(db_session):
    await db_session.execute(insert(Book).values(
        asin="B000PGNUL2", title="t", region="us",
        plans=type_coerce(None, JSONB(none_as_null=True))))

    row = (await db_session.execute(text(
        "select plans is null, jsonb_typeof(plans) from books"))).one()

    assert row == (True, None)
