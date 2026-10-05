"""
The stored lists behind max_age on a LocalStore over a real Postgres: a live
walk records its snapshot, a fresh one answers with no request, a future-dated
one is overwritten by the next live walk, and another region's books never
fill a snapshot. Needs Docker, through the container fixture in conftest.
"""

# Standard library
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Local
from libex_core.lookup import get_author_books, get_series_books
from libex_core.storage.read.walks import get_walk_result
from libex_core.storage.store import LocalStore
from libex_core.storage.walk_limits import SERIES_BOOKS
from libex_core.storage.write import write_walk_result
from tests.libex_core._lookup_support import AUTHOR, SERIES, asins, tick_walk_clock
from tests.libex_core.test_lookup_authors import WALK, _hydrating_get
from tests.libex_core.test_lookup_series_books import _series_members
from tests.libex_core.test_lookup_walks import assert_same_answer

pytestmark = pytest.mark.integration

DAY = timedelta(days=1)


@pytest.fixture(autouse=True)
def _walk_clock(monkeypatch):
    tick_walk_clock(monkeypatch)


@pytest_asyncio.fixture
async def store(postgres_url):
    name = "t_" + uuid.uuid4().hex[:12]
    engine = create_async_engine(postgres_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    async with engine.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    await engine.dispose()
    url = make_url(postgres_url).set(database=name).render_as_string(hide_password=False)
    local = LocalStore(url)
    await local.upgrade()
    await local.open()
    yield local
    await local.close()


def _no_requests():
    async def get(*args, **kwargs):
        raise AssertionError("a stored list must not ask Audible")
    return get


async def _row(store, region="us"):
    async with store.session() as session:
        return await get_walk_result(session, SERIES_BOOKS, SERIES, region)


async def _put(store, at, region="us", book_asins=None, complete=True):
    async with store.write() as session:
        return await write_walk_result(
            session, kind=SERIES_BOOKS, asin=SERIES, region=region,
            book_asins=book_asins, complete=complete, incomplete_reasons=[], at=at,
        )


async def test_a_live_walk_records_and_a_fresh_snapshot_answers_with_no_request(store):
    found = asins(3)
    await get_series_books(_series_members(found), SERIES, store=store)
    row = await _row(store)
    assert row["complete"] is True and row["book_asins"] == found
    result = await get_series_books(_no_requests(), SERIES, store=store, max_age=DAY)
    assert [b.asin for b in result.books] == found
    assert result.from_store == tuple(found) and result.snapshot_at is not None


async def test_an_incomplete_walk_replaces_the_row_and_is_never_served(store):
    found = asins(3)
    await get_series_books(_series_members(found), SERIES, store=store)
    await get_series_books(_series_members(found, stubs={found[1]}), SERIES, store=store)
    assert (await _row(store))["complete"] is False
    calls = []
    result = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, max_age=DAY
    )
    assert calls and result.snapshot_at is None


async def test_a_future_dated_row_is_never_served_and_the_next_walk_overwrites_it(store):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    assert await _put(store, datetime.now(timezone.utc) + timedelta(days=30), book_asins=found)
    calls = []
    result = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, max_age=timedelta.max
    )
    assert calls and result.snapshot_at is None
    assert (await _row(store))["confirmed_at"] < datetime.now(timezone.utc) + timedelta(minutes=1)
    again = await get_series_books(_no_requests(), SERIES, store=store, max_age=DAY)
    assert again.snapshot_at is not None


async def test_a_snapshot_naming_books_stored_only_for_another_region_is_a_miss(store):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store, region="us")
    assert await _put(store, datetime.now(timezone.utc), region="uk", book_asins=found)
    calls = []
    result = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, region="uk", max_age=DAY
    )
    assert calls and result.snapshot_at is None


async def test_a_stored_series_list_is_the_live_list_field_for_field(store):
    live = await get_series_books(_series_members(asins(4)), SERIES, store=store)
    stored = await get_series_books(_no_requests(), SERIES, store=store, max_age=DAY)
    assert stored.snapshot_at is not None
    assert_same_answer(live, stored)


async def test_a_stored_author_list_is_the_live_list_field_for_field(store, monkeypatch):
    found = asins(4)
    monkeypatch.setattr(WALK, AsyncMock(return_value=(list(found), True)))
    live = await get_author_books(_hydrating_get(), AUTHOR, store=store)
    stored = await get_author_books(_no_requests(), AUTHOR, store=store, max_age=DAY)
    assert stored.snapshot_at is not None
    assert_same_answer(live, stored)
