"""
confirmed_at on books, series and authors, and chapters_confirmed_at on books:
stamped only by a real Audible answer for the row's own entity, on SQLite and
on Postgres.

Every case starts from NULL (a row written without the flag), because the two
backends spell "the later of two" differently and only a NULL start tells a
correct spelling from SQLite's max(), which returns NULL when either argument
is. The clock the writers read is replaced, so a stamp set "later" or "earlier"
is exactly that.
"""

# Standard library
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Local
from libex_core.storage.base import Base
from libex_core.storage.models import Author, Book, Series
from libex_core.storage.store import LocalStore
from libex_core.storage.write import (
    confirm_chapters,
    upsert_author,
    upsert_series,
    write_author_profile,
    write_books,
    write_series_profile,
    write_track,
)
from tests.libex_core.storage._support import core_tables

pytest.importorskip("aiosqlite")

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)

BOOK = {
    "asin": "B0STAMP001", "region": "us", "title": "Stamped",
    "authors": [{"asin": "B0AUTHOR01", "name": "Ann Author", "region": "us"}],
    "series": [{"asin": "B0SERIES01", "name": "The Series", "position": "1"}],
}
SERIES = {"asin": "B0SERIES01", "name": "The Series", "region": "us", "description": "About"}
AUTHOR = {"asin": "B0AUTHOR01", "name": "Ann Author", "region": "us", "description": "Bio"}
CHAPTERS = {"chapters": [{"title": "One"}]}
EMPTY = {"chapters": []}


class _Backend:
    def __init__(self, write, read):
        self.write = write
        self.read = read

    async def stamp(self, model, **where):
        async with self.read() as session:
            rows = (await session.execute(select(model).filter_by(**where))).scalars().all()
            assert len(rows) == 1
            return rows[0]


@pytest.fixture
def clock(monkeypatch):
    """Sets the instant every writer stamps with."""
    now = {"value": T0}

    def set_to(value):
        now["value"] = value

    for module in ("books", "entities"):
        monkeypatch.setattr(
            f"libex_core.storage.write.{module}.utc_now", lambda: now["value"]
        )
    return set_to


@pytest_asyncio.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
async def backend(request):
    if request.param == "sqlite":
        store = LocalStore("sqlite+aiosqlite://")
        await store.upgrade()
        await store.open()
        yield _Backend(store.write, store.session)
        await store.close()
        return
    url = request.getfixturevalue("postgres_url")
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=core_tables())
        await conn.run_sync(Base.metadata.create_all, tables=core_tables())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def write():
        async with factory() as session:
            yield session
            await session.commit()

    @asynccontextmanager
    async def read():
        async with factory() as session:
            yield session

    yield _Backend(write, read)
    await engine.dispose()


async def _book(backend, **kw):
    async with backend.write() as session:
        await write_books(session, [BOOK], **kw)


# ============================================================
# BOOKS
# ============================================================

async def test_a_book_written_without_the_flag_starts_from_null(backend, clock):
    await _book(backend)
    book = await backend.stamp(Book, asin="B0STAMP001")
    assert book.confirmed_at is None
    assert book.chapters_confirmed_at is None


async def test_a_product_answer_stamps_the_book_from_null(backend, clock):
    await _book(backend)
    clock(T1)
    await _book(backend, confirm=True)
    assert (await backend.stamp(Book, asin="B0STAMP001")).confirmed_at == T1


async def test_a_first_write_with_the_flag_stamps_the_new_row(backend, clock):
    clock(T1)
    await _book(backend, confirm=True)
    assert (await backend.stamp(Book, asin="B0STAMP001")).confirmed_at == T1


async def test_an_unchanged_refetch_still_moves_the_stamp_forward(backend, clock):
    clock(T0)
    await _book(backend, confirm=True)
    clock(T2)
    await _book(backend, confirm=True)
    assert (await backend.stamp(Book, asin="B0STAMP001")).confirmed_at == T2


async def test_the_stamp_never_moves_backwards(backend, clock):
    clock(T2)
    await _book(backend, confirm=True)
    clock(T1)
    await _book(backend, confirm=True)
    assert (await backend.stamp(Book, asin="B0STAMP001")).confirmed_at == T2


async def test_a_write_that_confirms_nothing_leaves_the_stamp(backend, clock):
    clock(T1)
    await _book(backend, confirm=True)
    clock(T2)
    await _book(backend)
    assert (await backend.stamp(Book, asin="B0STAMP001")).confirmed_at == T1


async def test_a_stamp_is_per_region(backend, clock):
    clock(T1)
    async with backend.write() as session:
        await write_books(session, [BOOK, {**BOOK, "region": "uk"}])
    async with backend.write() as session:
        await write_books(session, [BOOK], confirm=True)
    assert (await backend.stamp(Book, asin="B0STAMP001", region="us")).confirmed_at == T1
    assert (await backend.stamp(Book, asin="B0STAMP001", region="uk")).confirmed_at is None


# ============================================================
# MENTIONS
# ============================================================

async def test_a_book_never_stamps_the_series_or_authors_it_names(backend, clock):
    clock(T1)
    await _book(backend, confirm=True)
    assert (await backend.stamp(Series, asin="B0SERIES01")).confirmed_at is None
    assert (await backend.stamp(Author, asin="B0AUTHOR01")).confirmed_at is None


async def test_a_mention_leaves_a_stamp_a_profile_fetch_set(backend, clock):
    clock(T1)
    async with backend.write() as session:
        await write_series_profile(session, SERIES, confirm=True)
        await write_author_profile(session, AUTHOR, confirm=True)
    clock(T2)
    await _book(backend, confirm=True)
    async with backend.write() as session:
        await upsert_series(session, {**SERIES, "description": "Longer description"})
        await upsert_author(session, AUTHOR)
    assert (await backend.stamp(Series, asin="B0SERIES01")).confirmed_at == T1
    assert (await backend.stamp(Author, asin="B0AUTHOR01")).confirmed_at == T1


# ============================================================
# SERIES AND AUTHORS
# ============================================================

async def test_a_series_profile_stamps_only_when_asked_and_never_backwards(backend, clock):
    clock(T1)
    async with backend.write() as session:
        await write_series_profile(session, SERIES)
    assert (await backend.stamp(Series, asin="B0SERIES01")).confirmed_at is None
    async with backend.write() as session:
        await write_series_profile(session, SERIES, confirm=True)
    assert (await backend.stamp(Series, asin="B0SERIES01")).confirmed_at == T1
    clock(T0)
    async with backend.write() as session:
        await write_series_profile(session, SERIES, confirm=True)
    assert (await backend.stamp(Series, asin="B0SERIES01")).confirmed_at == T1
    clock(T2)
    async with backend.write() as session:
        await write_series_profile(session, SERIES)
    assert (await backend.stamp(Series, asin="B0SERIES01")).confirmed_at == T1


async def test_an_author_profile_stamps_only_when_asked_and_never_backwards(backend, clock):
    clock(T1)
    async with backend.write() as session:
        await write_author_profile(session, AUTHOR)
    assert (await backend.stamp(Author, asin="B0AUTHOR01")).confirmed_at is None
    async with backend.write() as session:
        await write_author_profile(session, AUTHOR, confirm=True)
    assert (await backend.stamp(Author, asin="B0AUTHOR01")).confirmed_at == T1
    clock(T0)
    async with backend.write() as session:
        await write_author_profile(session, AUTHOR, confirm=True)
    assert (await backend.stamp(Author, asin="B0AUTHOR01")).confirmed_at == T1
    clock(T2)
    async with backend.write() as session:
        await write_author_profile(session, AUTHOR, confirm=True)
    assert (await backend.stamp(Author, asin="B0AUTHOR01")).confirmed_at == T2


async def test_an_author_first_written_by_a_profile_fetch_is_stamped(backend, clock):
    clock(T1)
    async with backend.write() as session:
        await write_author_profile(session, AUTHOR, confirm=True)
    assert (await backend.stamp(Author, asin="B0AUTHOR01")).confirmed_at == T1


# ============================================================
# CHAPTERS
# ============================================================

async def test_a_chapters_listing_stamps_the_books_chapters_when_asked(backend, clock):
    await _book(backend)
    clock(T1)
    async with backend.write() as session:
        await write_track(session, "B0STAMP001", CHAPTERS, region="us")
    assert (await backend.stamp(Book, asin="B0STAMP001")).chapters_confirmed_at is None
    async with backend.write() as session:
        assert await write_track(session, "B0STAMP001", CHAPTERS, region="us", confirm=True) == 1
    book = await backend.stamp(Book, asin="B0STAMP001")
    assert book.chapters_confirmed_at == T1
    assert book.confirmed_at is None


async def test_an_empty_chapters_answer_is_an_answer_and_stamps(backend, clock):
    await _book(backend)
    clock(T1)
    async with backend.write() as session:
        assert await confirm_chapters(session, "B0STAMP001", region="us") is True
    assert (await backend.stamp(Book, asin="B0STAMP001")).chapters_confirmed_at == T1


async def test_the_chapters_stamp_never_moves_backwards(backend, clock):
    await _book(backend)
    clock(T2)
    async with backend.write() as session:
        await confirm_chapters(session, "B0STAMP001", region="us")
    clock(T1)
    async with backend.write() as session:
        await confirm_chapters(session, "B0STAMP001", region="us")
        await write_track(session, "B0STAMP001", EMPTY, region="us", confirm=True)
    assert (await backend.stamp(Book, asin="B0STAMP001")).chapters_confirmed_at == T2


async def test_chapters_for_a_book_not_stored_for_the_region_stamp_nothing(backend, clock):
    await _book(backend)
    async with backend.write() as session:
        assert await confirm_chapters(session, "B0STAMP001", region="uk") is False
        assert await write_track(
            session, "B0STAMP001", CHAPTERS, region="uk", confirm=True
        ) is None
    assert (await backend.stamp(Book, asin="B0STAMP001")).chapters_confirmed_at is None
