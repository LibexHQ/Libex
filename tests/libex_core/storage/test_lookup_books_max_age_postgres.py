"""
max_age on get_book and get_books over a real Postgres: a fresh book answers
with no request, a stale or never-confirmed one and another region's are asked
for live, and a mixed bulk asks only for the rest. Needs Docker, through the
container fixture in conftest.
"""

# Standard library
import uuid
from datetime import datetime, timedelta, timezone

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Local
from libex_core.lookup import get_book, get_books
from libex_core.storage.models import Book
from libex_core.storage.store import LocalStore
from tests.libex_core.test_lookup_books_max_age import A, B, C, DAY, counting, requested, seed

pytestmark = pytest.mark.integration


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


async def _stamp(store, asin, at, region="us"):
    async with store.write() as session:
        await session.execute(
            update(Book).where(Book.asin == asin, Book.region == region).values(confirmed_at=at)
        )


async def test_a_fresh_book_is_served_with_no_request(store):
    await seed(store, A)
    get = counting(**{A: 1})
    assert (await get_book(get, A, store=store, max_age=DAY)).asin == A
    assert get.sent == []


async def test_a_mixed_bulk_asks_only_for_the_stale_and_missing(store):
    await seed(store, A, B)
    await _stamp(store, B, datetime.now(timezone.utc) - 3 * DAY)
    get = counting(**{A: 1, B: 1, C: 1})
    got = await get_books(get, [C, B, A], store=store, max_age=DAY)
    assert len(get.sent) == 1 and requested(get) == [B, C]
    assert [b.asin for b in got.books] == [C, B, A]


async def test_a_never_confirmed_and_a_future_dated_book_are_asked_for(store):
    await seed(store, A, B)
    await _stamp(store, A, None)
    await _stamp(store, B, datetime.now(timezone.utc) + 30 * DAY)
    get = counting(**{A: 1, B: 1})
    await get_books(get, [A, B], store=store, max_age=timedelta.max)
    assert requested(get) == [A, B]


async def test_another_regions_book_is_never_served(store):
    await seed(store, A, region="uk")
    get = counting(**{A: 1})
    await get_book(get, A, region="us", store=store, max_age=timedelta.max)
    assert get.sent
