"""
libex_core.lookup.ingest on a SQLite store: a Libex server's published models
written under the merge rules, with the settled values read back as unknown and
nothing confirmed. The cases are shared with the Postgres run.
"""

# Standard library
from unittest.mock import patch

# Third party
import pytest
import pytest_asyncio

# Local
from libex_core.lookup.ingest import ingest_books, ingest_chapters
from libex_core.models import BookResponse, ChapterResponse
from libex_core.storage.read import books as read_books
from libex_core.storage.store import LocalStore, StoreClosed
from tests.libex_core import _ingest_cases as cases


async def _open(path):
    local = LocalStore(f"sqlite+aiosqlite:///{path}")
    await local.upgrade()
    await local.open()
    return local


@pytest_asyncio.fixture
async def store(tmp_path):
    local = await _open(tmp_path / "one.db")
    yield local
    await local.close()


@pytest_asyncio.fixture
async def other(tmp_path):
    local = await _open(tmp_path / "two.db")
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


async def test_a_closed_store_raises_before_anything_is_written(tmp_path):
    local = await _open(tmp_path / "closed.db")
    await local.close()
    book = BookResponse(asin=cases.ASIN, region="us", title="T")
    with pytest.raises(StoreClosed):
        await ingest_books(local, [book], region="us")
    with pytest.raises(StoreClosed):
        await ingest_chapters(local, cases.ASIN, ChapterResponse(), region="us")


async def test_an_unknown_region_and_a_foreign_asin(store):
    book = BookResponse(asin=cases.ASIN, region="us", title="T")
    with pytest.raises(Exception):
        await ingest_books(store, [book], region="zz")
    odd = BookResponse(asin="not an asin", region="us", title="T")
    assert await ingest_books(store, [odd], region="us") == set()
    assert await ingest_books(store, [], region="us") == set()


async def test_ingest_never_replaces_a_stored_chapter_listing(store):
    await cases.case_ingest_never_replaces_a_stored_chapter_listing(store)


async def test_ingest_stores_only_what_the_listing_carries(store):
    await cases.case_ingest_stores_only_what_the_listing_carries(store)


async def test_a_nameless_author_or_narrator_does_not_lose_the_book(store):
    await cases.case_a_nameless_author_or_narrator_does_not_lose_the_book(store)


async def test_an_author_without_a_region_takes_the_ingest_region(store):
    await cases.case_an_author_without_a_region_takes_the_ingest_region(store)


async def test_a_failed_read_of_the_stored_listing_writes_nothing(store):
    await ingest_books(store, [await cases.response_of(cases.rich())], region="us")
    assert await ingest_chapters(store, cases.ASIN, cases.CHAPTERS, region="us") is True
    stored = (await cases.stored_chapters(store, cases.ASIN, region="us")).value

    other_listing = ChapterResponse(chapters=[{"title": "Replacement"}])
    with patch.object(read_books, "get_track", side_effect=RuntimeError("read failed")):
        assert await ingest_chapters(store, cases.ASIN, other_listing, region="us") is False
    assert (await cases.stored_chapters(store, cases.ASIN, region="us")).value == stored
