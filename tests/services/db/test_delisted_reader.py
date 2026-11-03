"""
get_delisted_books on SQLite: the hand-entered rows only, newest entry first
with a stable tiebreak, one entry per regional record, paged without a repeat
or a skip, each served in full exactly as get_book serves it. The guarded app
reader is held to the house failure behaviour. Postgres is covered in
tests/integration/test_manual_entry_endpoint.py.
"""

# Standard library
import logging
from datetime import datetime, timedelta, timezone

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from unittest.mock import AsyncMock

# Local
from app.services.db import stored
from app.services.db.reader import get_delisted_books_from_db
from libex_core.exceptions import LibexException
from libex_core.storage.models import Author, Book, Genre, Narrator, author_book, book_genre, book_narrator
from libex_core.storage.read.books import get_book, get_delisted_books
from libex_core.storage.store import LocalStore

pytest.importorskip("aiosqlite")

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def _row(asin, region="us", stamp=None, **kw):
    return Book(
        asin=asin, region=region, title=kw.pop("title", f"Title {asin}"), explicit=False,
        whisper_sync=False, has_pdf=False, is_listenable=False, is_buyable=False, is_vvab=False,
        manual_entry_at=stamp, created_at=T0, updated_at=T0, **kw,
    )


@pytest_asyncio.fixture
async def store():
    s = LocalStore("sqlite+aiosqlite://")
    await s.upgrade()
    await s.open()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def session(store):
    async with store.session() as s:
        yield s


async def _add(session, *rows):
    session.add_all(rows)
    await session.commit()


def _asins(books):
    return [(b["asin"], b["region"]) for b in books]


# ============================================================
# WHICH ROWS
# ============================================================


async def test_only_rows_with_a_manual_entry_stamp_are_listed(session):
    await _add(session,
               _row("B0LIVE0001"),
               _row("B0MANUAL01", stamp=T0),
               _row("B0RELIST01", stamp=T0 + timedelta(hours=1), confirmed_at=T0 + timedelta(days=1)))
    books = await get_delisted_books(session)
    assert _asins(books) == [("B0RELIST01", "us"), ("B0MANUAL01", "us")]
    assert all(b["manualEntry"] is True for b in books)


async def test_a_relisted_row_stays_in_the_list_with_manual_entry_true(session):
    await _add(session, _row("B0RELIST01", stamp=T0, confirmed_at=T0 + timedelta(days=1), title="Audible's title"))
    [book] = await get_delisted_books(session)
    assert book["title"] == "Audible's title" and book["manualEntry"] is True


async def test_an_empty_store_and_a_store_of_live_rows_list_nothing(session):
    assert await get_delisted_books(session) == []
    await _add(session, _row("B0LIVE0001"), _row("B0LIVE0002", region="uk"))
    assert await get_delisted_books(session) == []


async def test_every_regional_record_is_its_own_entry(session):
    await _add(session,
               _row("B0MANUAL01", "us", stamp=T0),
               _row("B0MANUAL01", "uk", stamp=T0 + timedelta(minutes=1), is_primary=False),
               _row("B0MANUAL01", "de", stamp=None, is_primary=False))
    assert _asins(await get_delisted_books(session)) == [("B0MANUAL01", "uk"), ("B0MANUAL01", "us")]


async def test_the_region_filter_keeps_only_that_region(session):
    await _add(session,
               _row("B0MANUAL01", "us", stamp=T0),
               _row("B0MANUAL02", "uk", stamp=T0),
               _row("B0MANUAL03", "jp", stamp=T0))
    assert _asins(await get_delisted_books(session, region="uk")) == [("B0MANUAL02", "uk")]
    assert await get_delisted_books(session, region="br") == []
    assert len(await get_delisted_books(session)) == 3


# ============================================================
# ORDER AND PAGING
# ============================================================


async def test_newest_entry_first_then_asin_then_region(session):
    await _add(session,
               _row("B0AAAAAAA2", "us", stamp=T0),
               _row("B0AAAAAAA1", "us", stamp=T0),
               _row("B0AAAAAAA1", "uk", stamp=T0),
               _row("B0ZZZZZZZZ", "us", stamp=T0 + timedelta(days=1)),
               _row("B0OLDEST01", "us", stamp=T0 - timedelta(days=1)))
    assert _asins(await get_delisted_books(session)) == [
        ("B0ZZZZZZZZ", "us"), ("B0AAAAAAA1", "uk"), ("B0AAAAAAA1", "us"),
        ("B0AAAAAAA2", "us"), ("B0OLDEST01", "us"),
    ]


async def test_order_alone_does_not_reverse_the_default(session):
    await _add(session, _row("B0OLDER001", stamp=T0), _row("B0NEWER001", stamp=T0 + timedelta(days=1)))
    assert _asins(await get_delisted_books(session, order="asc")) == _asins(await get_delisted_books(session))


async def test_pages_never_repeat_or_skip_a_book_even_when_stamps_tie(session):
    await _add(session, *[_row(f"B0PAGE{i:04d}", stamp=T0) for i in (3, 6, 0, 5, 2, 4, 1)])
    seen = []
    for page in (1, 2, 3, 4):
        seen += _asins(await get_delisted_books(session, limit=3, page=page))
    assert len(seen) == 7 and len(set(seen)) == 7
    assert seen == sorted(seen)
    assert seen == _asins(await get_delisted_books(session, limit=100))


async def test_a_page_past_the_end_is_an_empty_list(session):
    await _add(session, _row("B0MANUAL01", stamp=T0))
    assert len(await get_delisted_books(session, limit=5, page=1)) == 1
    assert await get_delisted_books(session, limit=5, page=2) == []


async def test_the_limit_bounds_the_page(session):
    await _add(session, *[_row(f"B0LIMT{i:04d}", stamp=T0) for i in range(4)])
    assert len(await get_delisted_books(session, limit=2)) == 2


async def test_a_sort_field_replaces_the_default_order_and_ties_still_break_stably(session):
    await _add(session,
               _row("B0MANUAL01", stamp=T0, title="Charlie"),
               _row("B0MANUAL02", stamp=T0 + timedelta(days=1), title="Alpha"),
               _row("B0MANUAL03", stamp=T0 + timedelta(days=2), title="Bravo"),
               _row("B0MANUAL04", stamp=T0 + timedelta(days=3), title="Bravo"))
    asc = [b["title"] for b in await get_delisted_books(session, sort="title", order="asc")]
    desc = [b["title"] for b in await get_delisted_books(session, sort="title", order="desc")]
    assert asc == ["Alpha", "Bravo", "Bravo", "Charlie"]
    assert desc == ["Charlie", "Bravo", "Bravo", "Alpha"]
    tied = await get_delisted_books(session, sort="title", order="asc")
    assert [b["asin"] for b in tied[1:3]] == ["B0MANUAL04", "B0MANUAL03"]


async def test_an_unknown_sort_field_falls_back_to_newest_first(session):
    await _add(session, _row("B0OLDER001", stamp=T0), _row("B0NEWER001", stamp=T0 + timedelta(days=1)))
    assert _asins(await get_delisted_books(session, sort="nope")) == [("B0NEWER001", "us"), ("B0OLDER001", "us")]


# ============================================================
# EACH ITEM IS THE FULL STORED RECORD
# ============================================================


async def test_each_item_equals_what_get_book_serves_with_every_pivot(session):
    await _add(session, _row("B0MANUAL01", stamp=T0, description="typed in"))
    author = (await session.execute(insert(Author).values(
        asin="B000APF21M", region="us", name="An Author", created_at=T0, updated_at=T0).returning(Author.id))).scalar_one()
    await session.execute(insert(Narrator).values(name="A Narrator"))
    await session.execute(insert(Genre).values(asin="123456", name="Fantasy", type="Genres"))
    await session.execute(insert(author_book).values(author_id=author, book_asin="B0MANUAL01", book_region="us"))
    await session.execute(insert(book_narrator).values(narrator_name="A Narrator", book_asin="B0MANUAL01", book_region="us"))
    await session.execute(insert(book_genre).values(genre_asin="123456", book_asin="B0MANUAL01", book_region="us"))
    await session.commit()

    [listed] = await get_delisted_books(session)
    single = await get_book(session, "B0MANUAL01", region="us")
    assert listed == single
    assert [a["name"] for a in listed["authors"]] == ["An Author"]
    assert [n["name"] for n in listed["narrators"]] == ["A Narrator"]
    assert [g["name"] for g in listed["genres"]] == ["Fantasy"]
    assert listed["manualEntry"] is True


# ============================================================
# THE APP READERS
# ============================================================


async def test_the_guarded_reader_answers_an_empty_list_on_a_database_error():
    session = AsyncMock()
    session.execute.side_effect = Exception("DB error")
    assert await get_delisted_books_from_db(session) == []


async def test_the_guarded_reader_logs_the_failure_by_type_only(caplog):
    session = AsyncMock()
    session.execute.side_effect = IntegrityError("INSERT SECRET-ROW", {"x": "SECRET-ROW"}, Exception("dup"))
    with caplog.at_level(logging.WARNING):
        assert await get_delisted_books_from_db(session) == []
    records = [r for r in caplog.records if "DB read failed for delisted books" in r.getMessage()]
    assert len(records) == 1
    assert records[0].error_type == "IntegrityError"


async def test_the_raising_reader_lets_the_error_through():
    session = AsyncMock()
    session.execute.side_effect = RuntimeError("boom")
    with pytest.raises(LibexException):
        await stored.get_delisted_books_from_db(session)
