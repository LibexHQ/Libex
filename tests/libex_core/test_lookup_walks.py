"""
The stored lists behind max_age on get_series_books and get_author_books: what
a live walk records, when a record answers without a request, and that a record
that is not whole, fresh and well formed is never served. A real SQLite store
and a stand-in `get`; nothing touches a network.
"""

# Standard library
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import update

# Local
from libex_core.exceptions import NotFoundException
from libex_core.lookup import get_author_books, get_series_books
from libex_core.storage.models import WalkResult
from libex_core.storage.read.walks import get_walk_result
from libex_core.storage.store import LocalStore
from libex_core.storage.walk_limits import AUTHOR_BOOKS, SERIES_BOOKS
from tests.libex_core._lookup_support import AUTHOR, SERIES, asins
from tests.libex_core.test_lookup_authors import WALK, _hydrating_get
from tests.libex_core.test_lookup_series_books import _series_members

DAY = timedelta(days=1)


@pytest_asyncio.fixture
async def store(tmp_path):
    local = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    await local.upgrade()
    await local.open()
    yield local
    await local.close()


async def _row(store, kind, asin, region="us"):
    async with store.session() as session:
        return await get_walk_result(session, kind, asin, region)


async def _patch_row(store, **values):
    async with store.write() as session:
        await session.execute(update(WalkResult).values(**values))


def _no_requests():
    async def get(*args, **kwargs):
        raise AssertionError("a stored list must not ask Audible")
    return get


async def test_a_live_series_walk_records_a_complete_snapshot_in_served_order(store):
    found = asins(3)
    result = await get_series_books(
        _series_members(found), SERIES, store=store, sort="title", order="desc"
    )
    row = await _row(store, SERIES_BOOKS, SERIES)
    assert row["complete"] is True and row["book_asins"] == found
    assert result.snapshot_at is None and result.store_write_failed is False


async def test_a_live_author_walk_records_a_snapshot(store, monkeypatch):
    found = asins(3)
    monkeypatch.setattr(WALK, AsyncMock(return_value=(list(found), True)))
    await get_author_books(_hydrating_get(), AUTHOR, store=store)
    row = await _row(store, AUTHOR_BOOKS, AUTHOR)
    assert row["complete"] is True and row["book_asins"] == found


async def test_a_fresh_snapshot_answers_with_no_request(store):
    found = asins(3)
    await get_series_books(_series_members(found), SERIES, store=store)
    result = await get_series_books(_no_requests(), SERIES, store=store, max_age=DAY)
    assert [b.asin for b in result.books] == found
    assert result.complete is True and result.incomplete_reasons == ()
    assert result.from_store == tuple(found) and result.explicit_nulls == {}
    assert result.snapshot_at is not None and result.store_write_failed is False


async def test_a_stored_list_is_filtered_and_sorted_after_the_fact(store):
    found = asins(4)
    await get_series_books(_series_members(found), SERIES, store=store)
    result = await get_series_books(
        _no_requests(), SERIES, store=store, max_age=DAY, sort="title", order="desc"
    )
    assert len(result.books) == 4
    assert result.from_store == tuple(found), "from_store is every book, before shaping"


async def test_without_max_age_the_snapshot_is_never_read(store):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    calls = []
    await get_series_books(_series_members(found, calls=calls), SERIES, store=store)
    assert calls, "no max_age always goes live"


async def test_a_stale_snapshot_goes_live_and_is_replaced(store):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    await _patch_row(store, confirmed_at=datetime.now(timezone.utc) - 3 * DAY)
    calls = []
    result = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, max_age=DAY
    )
    assert calls and result.snapshot_at is None
    assert (await _row(store, SERIES_BOOKS, SERIES))["confirmed_at"] > (
        datetime.now(timezone.utc) - DAY
    )


async def test_an_incomplete_walk_replaces_the_row_and_is_never_served(store):
    found = asins(3)
    await get_series_books(_series_members(found), SERIES, store=store)
    await get_series_books(
        _series_members(found, stubs={found[1]}), SERIES, store=store
    )
    row = await _row(store, SERIES_BOOKS, SERIES)
    assert row["complete"] is False
    assert row["incomplete_reasons"] == ["hydration-not-found"]
    calls = []
    result = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, max_age=DAY
    )
    assert calls and result.snapshot_at is None


async def test_a_snapshot_book_missing_from_the_store_goes_live(store):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    await _patch_row(store, book_asins=[*found, "B0NOTSTORE"])
    calls = []
    result = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, max_age=DAY
    )
    assert calls and result.snapshot_at is None


@pytest.mark.parametrize("region", ["uk", "de", "jp", "ca", "au", "fr", "it", "es", "in", "br"])
async def test_a_snapshot_is_never_served_for_another_region(store, region):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store, region="us")
    calls = []
    result = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, region=region, max_age=DAY
    )
    assert calls and result.snapshot_at is None


@pytest.mark.parametrize(
    "bad",
    [
        {"book_asins": {"a": 1}},
        {"book_asins": "B0LOK00000"},
        {"book_asins": []},
        {"book_asins": ["b0lok0000"]},
        {"book_asins": ["B0LOK0000"]},
        {"book_asins": ["B0LOK00000\n"]},
        {"book_asins": ["ﬃ" * 3]},
        {"book_asins": [1]},
        {"confirmed_at": datetime.now(timezone.utc) + 3 * DAY},
    ],
)
async def test_a_malformed_or_future_row_is_a_miss(store, bad):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    await _patch_row(store, **bad)
    calls = []
    result = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, max_age=DAY
    )
    assert calls and result.snapshot_at is None
    assert (await _row(store, SERIES_BOOKS, SERIES))["complete"] is True, "overwritten"


async def test_a_huge_max_age_does_not_overflow(store):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    result = await get_series_books(
        _no_requests(), SERIES, store=store, max_age=timedelta.max
    )
    assert result.snapshot_at is not None


async def test_a_failed_read_goes_live(store, monkeypatch):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)

    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("libex_core.storage.read.walks.get_walk_result", broken)
    calls = []
    result = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, max_age=DAY
    )
    assert calls and result.snapshot_at is None


async def test_a_failed_snapshot_write_sets_store_write_failed(store, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("libex_core.storage.write.walks.write_walk_result", broken)
    result = await get_series_books(_series_members(asins(2)), SERIES, store=store)
    assert result.store_write_failed is True and len(result.books) == 2


async def test_a_discovery_404_deletes_the_snapshot_and_raises(store):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)

    async def empty(region, path, params=None, extra_headers=None):
        return {"response_groups": [], "product": {"relationships": []}}

    with pytest.raises(NotFoundException):
        await get_series_books(empty, SERIES, store=store)
    assert await _row(store, SERIES_BOOKS, SERIES) is None


async def test_an_outage_leaves_the_snapshot_alone(store):
    from libex_core.exceptions import AudibleAPIException

    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    before = await _row(store, SERIES_BOOKS, SERIES)

    async def down(*args, **kwargs):
        raise AudibleAPIException("down", upstream_status=503)

    with pytest.raises(AudibleAPIException):
        await get_series_books(down, SERIES, store=store)
    assert await _row(store, SERIES_BOOKS, SERIES) == before


@pytest.mark.parametrize("bad", [0, -1, "1", 5, timedelta(0), timedelta(seconds=-1)])
async def test_max_age_must_be_a_positive_timedelta(store, bad):
    with pytest.raises(ValueError):
        await get_series_books(_no_requests(), SERIES, store=store, max_age=bad)


async def test_max_age_without_a_store_is_a_value_error():
    with pytest.raises(ValueError):
        await get_series_books(_no_requests(), SERIES, max_age=DAY)
    with pytest.raises(ValueError):
        await get_author_books(_no_requests(), AUTHOR, max_age=DAY)


@pytest.mark.parametrize(
    "raw",
    [
        {"complete": 1},
        {"complete": "true"},
        {"confirmed_at": None},
        {"confirmed_at": datetime.now()},
        {"confirmed_at": "2026-10-04"},
        {"book_asins": None},
    ],
)
async def test_a_raw_row_the_column_would_not_have_coerced_is_a_miss(store, monkeypatch, raw):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    row = {**await _row(store, SERIES_BOOKS, SERIES), **raw}
    monkeypatch.setattr(
        "libex_core.lookup._store.stored_walk", AsyncMock(return_value=row)
    )
    calls = []
    result = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, max_age=DAY
    )
    assert calls and result.snapshot_at is None
