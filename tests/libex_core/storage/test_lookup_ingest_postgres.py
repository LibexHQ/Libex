"""
libex_core.lookup.ingest over a real Postgres, running the cases the SQLite
file runs. Needs Docker, through the container fixture in conftest.
"""

# Standard library
import uuid

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Local
from libex_core.storage.store import LocalStore
from tests.libex_core import _ingest_cases as cases

pytestmark = pytest.mark.integration


async def _database(postgres_url):
    name = "t_" + uuid.uuid4().hex[:12]
    engine = create_async_engine(postgres_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    async with engine.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    await engine.dispose()
    url = make_url(postgres_url).set(database=name).render_as_string(hide_password=False)
    local = LocalStore(url)
    await local.upgrade()
    await local.open()
    return local


@pytest_asyncio.fixture
async def store(postgres_url):
    local = await _database(postgres_url)
    yield local
    await local.close()


@pytest_asyncio.fixture
async def other(postgres_url):
    local = await _database(postgres_url)
    yield local
    await local.close()


async def test_a_round_trip_equals_writing_the_unsettled_dict(store, other):
    await cases.case_round_trip_equals_writing_the_unsettled_dict(store, other)


async def test_a_settled_default_never_overwrites_an_asserted_value(store):
    await cases.case_a_settled_default_never_overwrites_an_asserted_value(store)


async def test_an_asserted_value_still_lands(store):
    await cases.case_an_asserted_value_still_lands(store)


async def test_ingest_never_shrinks_a_richer_row(store):
    await cases.case_ingest_never_shrinks_a_richer_row(store)


async def test_nothing_is_confirmed(store):
    await cases.case_nothing_is_confirmed(store)


async def test_regions_are_kept_apart(store):
    await cases.case_regions_are_kept_apart(store)


async def test_chapters(store):
    await cases.case_chapters(store)


async def test_series_and_author(store):
    await cases.case_series_and_author(store)


async def test_ingest_never_replaces_a_stored_chapter_listing(store):
    await cases.case_ingest_never_replaces_a_stored_chapter_listing(store)


async def test_ingest_stores_only_what_the_listing_carries(store):
    await cases.case_ingest_stores_only_what_the_listing_carries(store)


async def test_a_nameless_author_or_narrator_does_not_lose_the_book(store):
    await cases.case_a_nameless_author_or_narrator_does_not_lose_the_book(store)


async def test_an_author_without_a_region_takes_the_ingest_region(store):
    await cases.case_an_author_without_a_region_takes_the_ingest_region(store)
