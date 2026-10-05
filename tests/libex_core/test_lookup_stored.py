"""
libex_core.lookup.stored: reading what a LocalStore holds, and the confirmation
stamps the live lookups leave behind.

The stored reads run against a real SQLite store written through the live
lookups, so the shape they return is compared with what the live lookup itself
returns for the same Audible answer. The stamps are checked from the outside:
a real answer sets them, and an outage, an answer the store itself gave, a
placeholder, a 404 for the book or a mention does not.
"""

# Standard library
from datetime import datetime, timedelta, timezone

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import select, update

# Local
from libex_core.audible.books import UNRELEASED_PLACEHOLDER
from libex_core.exceptions import AudibleAPIException, NotFoundException
from libex_core.lookup import (
    get_author,
    get_book,
    get_books,
    get_chapters,
    get_series,
    new_releases,
)
from libex_core.lookup.stored import (
    Stored,
    chapters_confirmed_at,
    stored_author,
    stored_book,
    stored_books,
    stored_chapters,
    stored_series,
)
from libex_core.models import AuthorResponse, BookResponse, ChapterResponse, SeriesResponse
from libex_core.storage.models import Author, Book, Series
from libex_core.storage.store import LocalStore, StoreClosed
from tests.libex_core._lookup_support import (
    AUTHOR,
    SERIES,
    fake_get,
    not_found_get,
    outage_get,
    product,
)
from tests.libex_core.test_lookup_store import CHAPTERS, EMPTY_LISTING, batch_get, chapter_get

ASIN = "B0STORE001"
OTHER = "B0STORE002"
SERIES_RELATION = {
    "relationship_type": "series",
    "relationship_to_product": "parent",
    "asin": SERIES,
    "title": "The Series",
    "sequence": "1",
}


@pytest_asyncio.fixture
async def store(tmp_path):
    local = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    await local.upgrade()
    await local.open()
    yield local
    await local.close()


async def stamp(store, model, column, **where):
    async with store.session() as session:
        rows = (await session.execute(select(getattr(model, column)).filter_by(**where))).all()
    assert len(rows) == 1
    return rows[0][0]


async def stored_row_count(store, model):
    async with store.session() as session:
        return len((await session.execute(select(model))).all())


# ============================================================
# THE STORED READS
# ============================================================

async def test_a_stored_book_is_the_book_the_live_lookup_served(store):
    live = await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)

    found = await stored_book(store, ASIN, region="us")

    assert isinstance(found, Stored)
    assert found.region == "us"
    assert isinstance(found.value, BookResponse)
    assert found.value == live
    assert found.confirmed_at is not None


async def test_confirmed_at_stays_in_the_wrapper_and_out_of_the_book(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)

    found = await stored_book(store, ASIN, region="us")

    assert "confirmed_at" not in found.value.model_dump()
    assert "confirmedAt" not in found.value.model_dump()
    assert not hasattr(found.value, "confirmed_at")


async def test_a_book_that_is_not_stored_is_none(store):
    assert await stored_book(store, ASIN, region="us") is None
    assert await stored_books(store, [ASIN, OTHER], region="us") == []


async def test_a_value_that_cannot_be_an_asin_is_a_miss(store):
    assert await stored_book(store, "not an asin", region="us") is None
    assert await stored_series(store, "", region="us") is None
    assert await stored_author(store, "x", region="us") is None
    assert await stored_chapters(store, "x", region="us") is None
    assert await stored_books(store, ["bad", 5], region="us") == []


async def test_stored_books_keep_the_order_asked_and_leave_misses_out(store):
    get = batch_get(**{ASIN: product(ASIN), OTHER: product(OTHER)})
    await get_books(get, [ASIN, OTHER], store=store)

    found = await stored_books(store, [OTHER, "B0MISSING0", ASIN.lower()], region="us")

    assert [s.value.asin for s in found] == [OTHER, ASIN]
    assert all(s.region == "us" for s in found)


async def test_a_book_is_read_for_its_own_region_only(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, region="us", store=store)

    assert await stored_book(store, ASIN, region="uk") is None
    assert await stored_books(store, [ASIN], region="de") == []
    assert (await stored_book(store, ASIN, region="us")).region == "us"


async def test_the_same_asin_in_two_regions_carries_each_regions_own_stamp(store, monkeypatch):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, region="us", store=store)
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, region="uk", store=store)
    long_ago = datetime(2020, 1, 1, tzinfo=timezone.utc)
    async with store.write() as session:
        await session.execute(
            update(Book).where(Book.region == "uk").values(confirmed_at=long_ago)
        )

    uk = await stored_book(store, ASIN, region="uk")
    us = await stored_book(store, ASIN, region="us")

    assert uk.confirmed_at == long_ago
    assert us.confirmed_at > long_ago


async def test_the_region_is_required_and_checked(store):
    with pytest.raises(TypeError):
        await stored_book(store, ASIN)
    with pytest.raises(TypeError):
        await stored_books(store, [ASIN])
    with pytest.raises(TypeError):
        await stored_chapters(store, ASIN)
    with pytest.raises(TypeError):
        await stored_series(store, SERIES)
    with pytest.raises(TypeError):
        await stored_author(store, AUTHOR)
    with pytest.raises(Exception) as raised:
        await stored_book(store, ASIN, region="zz")
    assert type(raised.value).__name__ == "RegionException"


async def test_stored_chapters_are_the_listing_the_live_lookup_served(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    live = await get_chapters(chapter_get(CHAPTERS), ASIN, store=store)

    found = await stored_chapters(store, ASIN, region="us")

    assert isinstance(found.value, ChapterResponse)
    assert found.value == live
    assert found.region == "us"
    assert found.confirmed_at is not None
    assert await stored_chapters(store, ASIN, region="uk") is None


async def test_chapters_never_written_are_none_even_when_an_empty_answer_was_recorded(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    with pytest.raises(NotFoundException):
        await get_chapters(not_found_get, ASIN, store=store)

    assert await stored_chapters(store, ASIN, region="us") is None


async def test_chapters_confirmed_at_reads_the_stamp_of_a_listing(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    await get_chapters(chapter_get(CHAPTERS), ASIN, store=store)

    found = await chapters_confirmed_at(store, ASIN, region="us")

    assert found == (await stored_chapters(store, ASIN, region="us")).confirmed_at
    assert found is not None


async def test_chapters_confirmed_at_speaks_for_an_empty_answer(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    with pytest.raises(NotFoundException):
        await get_chapters(not_found_get, ASIN, store=store)

    assert await stored_chapters(store, ASIN, region="us") is None
    assert await chapters_confirmed_at(store, ASIN, region="us") is not None


async def test_chapters_confirmed_at_is_none_when_never_asked(store):
    assert await chapters_confirmed_at(store, ASIN, region="us") is None
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    assert await chapters_confirmed_at(store, ASIN, region="us") is None
    assert await chapters_confirmed_at(store, "not an asin", region="us") is None


async def test_chapters_confirmed_at_is_the_region_asked_and_required(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    await get_chapters(chapter_get(CHAPTERS), ASIN, store=store)

    assert await chapters_confirmed_at(store, ASIN, region="uk") is None
    with pytest.raises(TypeError):
        await chapters_confirmed_at(store, ASIN)
    with pytest.raises(Exception) as raised:
        await chapters_confirmed_at(store, ASIN, region="zz")
    assert type(raised.value).__name__ == "RegionException"


async def test_chapters_confirmed_at_on_a_closed_store_raises(store):
    await store.close()
    with pytest.raises(StoreClosed):
        await chapters_confirmed_at(store, ASIN, region="us")


async def test_a_stored_series_is_the_series_the_live_lookup_served(store):
    live = await get_series(fake_get, SERIES, store=store)

    found = await stored_series(store, SERIES, region="us")

    assert isinstance(found.value, SeriesResponse)
    assert found.value == live
    assert found.confirmed_at is not None
    assert await stored_series(store, SERIES, region="uk") is None
    assert await stored_series(store, "B0SERIES99", region="us") is None


async def test_a_stored_author_is_the_author_the_live_lookup_served(store):
    live = await get_author(fake_get, AUTHOR, store=store)

    found = await stored_author(store, AUTHOR, region="us")

    assert isinstance(found.value, AuthorResponse)
    assert found.value == live
    assert found.confirmed_at is not None
    assert await stored_author(store, AUTHOR, region="uk") is None


async def test_a_closed_store_raises_before_anything_is_read(store):
    await store.close()

    for call in (
        stored_book(store, ASIN, region="us"),
        stored_books(store, [ASIN], region="us"),
        stored_chapters(store, ASIN, region="us"),
        stored_series(store, SERIES, region="us"),
        stored_author(store, AUTHOR, region="us"),
    ):
        with pytest.raises(StoreClosed):
            await call


async def test_a_closed_store_raises_even_for_a_value_that_cannot_be_an_asin(store):
    await store.close()
    with pytest.raises(StoreClosed):
        await stored_book(store, "nope", region="us")
    with pytest.raises(StoreClosed):
        await stored_books(store, [], region="us")


# ============================================================
# WHAT SETS A STAMP
# ============================================================

async def test_a_product_fetch_stamps_the_book_and_not_what_it_names(store):
    book = product(ASIN, relationships=[SERIES_RELATION])
    await get_book(batch_get(**{ASIN: book}), ASIN, store=store)

    assert await stamp(store, Book, "confirmed_at", asin=ASIN) is not None
    assert await stamp(store, Series, "confirmed_at", asin=SERIES) is None
    assert await stamp(store, Author, "confirmed_at", asin=AUTHOR) is None


async def test_a_series_profile_fetch_and_an_author_profile_fetch_stamp_their_own(store):
    await get_series(fake_get, SERIES, store=store)
    await get_author(fake_get, AUTHOR, store=store)

    assert await stamp(store, Series, "confirmed_at", asin=SERIES) is not None
    assert await stamp(store, Author, "confirmed_at", asin=AUTHOR) is not None


async def test_a_listing_leaves_the_books_it_stores_unstamped(store):
    await new_releases(fake_get, store=store)

    async with store.session() as session:
        stamps = [row[0] for row in (await session.execute(select(Book.confirmed_at))).all()]
    assert stamps
    assert all(s is None for s in stamps)


async def test_a_chapters_listing_stamps_the_chapters(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    assert await stamp(store, Book, "chapters_confirmed_at", asin=ASIN) is None

    await get_chapters(chapter_get(CHAPTERS), ASIN, store=store)

    assert await stamp(store, Book, "chapters_confirmed_at", asin=ASIN) is not None


@pytest.mark.parametrize("answer", ["404", "no listing"])
async def test_a_legitimately_empty_chapters_answer_stamps_the_chapters(store, answer):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)

    get = not_found_get if answer == "404" else chapter_get({"content_metadata": {}})
    with pytest.raises(NotFoundException):
        await get_chapters(get, ASIN, store=store)

    assert await stamp(store, Book, "chapters_confirmed_at", asin=ASIN) is not None
    assert await stamp(store, Book, "confirmed_at", asin=ASIN) is not None


async def test_an_empty_listing_never_replaces_stored_chapters_and_still_stamps(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    await get_chapters(chapter_get(CHAPTERS), ASIN, store=store)
    when = datetime(2020, 1, 1, tzinfo=timezone.utc)
    async with store.write() as session:
        await session.execute(update(Book).values(chapters_confirmed_at=when))

    await get_chapters(chapter_get(EMPTY_LISTING), ASIN, store=store)

    found = await stored_chapters(store, ASIN, region="us")
    assert len(found.value.chapters) == 2
    assert found.confirmed_at > when


# ============================================================
# WHAT LEAVES A STAMP ALONE
# ============================================================

async def _stamped_book(store, when):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    async with store.write() as session:
        await session.execute(
            update(Book).where(Book.asin == ASIN)
            .values(confirmed_at=when, chapters_confirmed_at=when)
        )


async def test_an_outage_answered_from_the_store_does_not_move_the_stamp(store):
    when = datetime(2020, 1, 1, tzinfo=timezone.utc)
    await _stamped_book(store, when)
    await get_chapters(chapter_get(CHAPTERS), ASIN, store=store)
    async with store.write() as session:
        await session.execute(update(Book).values(chapters_confirmed_at=when))

    served = await get_book(outage_get, ASIN, store=store)
    chapters = await get_chapters(outage_get, ASIN, store=store)

    assert served.asin == ASIN and chapters.chapters
    assert await stamp(store, Book, "confirmed_at", asin=ASIN) == when
    assert await stamp(store, Book, "chapters_confirmed_at", asin=ASIN) == when


async def test_an_outage_with_nothing_stored_stamps_and_stores_nothing(store):
    with pytest.raises(AudibleAPIException):
        await get_book(outage_get, ASIN, store=store)
    with pytest.raises(AudibleAPIException):
        await get_chapters(outage_get, ASIN, store=store)
    with pytest.raises(AudibleAPIException):
        await get_series(outage_get, SERIES, store=store)

    assert await stored_row_count(store, Book) == 0
    assert await stored_row_count(store, Series) == 0


async def test_a_chapters_outage_does_not_stamp_the_chapters(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    with pytest.raises(AudibleAPIException):
        await get_chapters(outage_get, ASIN, store=store)

    assert await stamp(store, Book, "chapters_confirmed_at", asin=ASIN) is None


async def test_a_404_for_a_book_does_not_stamp_the_stored_row(store):
    when = datetime(2020, 1, 1, tzinfo=timezone.utc)
    await _stamped_book(store, when)

    with pytest.raises(NotFoundException):
        await get_book(not_found_get, ASIN, store=store)
    with pytest.raises(NotFoundException):
        await get_book(batch_get(), ASIN, store=store)

    assert await stamp(store, Book, "confirmed_at", asin=ASIN) == when


async def test_a_placeholder_record_is_neither_stored_nor_stamped(store):
    placeholder = product(ASIN, publication_datetime=UNRELEASED_PLACEHOLDER)
    with pytest.raises(NotFoundException):
        await get_book(batch_get(**{ASIN: placeholder}), ASIN, store=store)

    assert await stored_row_count(store, Book) == 0


async def test_a_placeholder_leaves_a_stored_books_stamp_alone(store):
    when = datetime(2020, 1, 1, tzinfo=timezone.utc)
    await _stamped_book(store, when)

    placeholder = product(ASIN, publication_datetime=UNRELEASED_PLACEHOLDER)
    with pytest.raises(NotFoundException):
        await get_book(batch_get(**{ASIN: placeholder}), ASIN, store=store)

    assert await stamp(store, Book, "confirmed_at", asin=ASIN) == when


async def test_a_fresh_fetch_moves_the_stamp_forward_and_never_back(store):
    future = datetime.now(timezone.utc) + timedelta(days=365)
    await _stamped_book(store, future)

    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    assert await stamp(store, Book, "confirmed_at", asin=ASIN) == future

    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    async with store.write() as session:
        await session.execute(update(Book).values(confirmed_at=past))
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    assert await stamp(store, Book, "confirmed_at", asin=ASIN) > past
