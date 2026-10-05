"""
The stored lists behind max_age on get_series_books and get_author_books: what
a live walk records, when a record answers without a request, and that a record
that is not whole, fresh and well formed is never served. A real SQLite store
and a stand-in `get`; nothing touches a network.
"""

# Standard library
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import update

# Local
from libex_core.exceptions import AudibleAPIException, NotFoundException
from libex_core.lookup import _store, _walks, get_author_books, get_series_books
from libex_core.storage.models import WalkResult
from libex_core.storage.read.walks import get_walk_result
from libex_core.storage.store import LocalStore
from libex_core.storage.walk_limits import AUTHOR_BOOKS, MAX_WALK_ASINS, SERIES_BOOKS
from libex_core.storage.write import write_walk_result
from tests.libex_core._lookup_support import AUTHOR, SERIES, asins, tick_walk_clock
from tests.libex_core.test_lookup_authors import WALK, _hydrating_get
from tests.libex_core.test_lookup_series_books import _series_members

DAY = timedelta(days=1)


@pytest.fixture(autouse=True)
def _walk_clock(monkeypatch):
    tick_walk_clock(monkeypatch)


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


# Section: author walks end to end


async def test_a_fresh_author_snapshot_answers_with_no_request(store, monkeypatch):
    found = asins(3)
    monkeypatch.setattr(WALK, AsyncMock(return_value=(list(found), True)))
    await get_author_books(_hydrating_get(), AUTHOR, store=store)
    walk = AsyncMock(side_effect=AssertionError("a stored list must not walk"))
    monkeypatch.setattr(WALK, walk)
    result = await get_author_books(_no_requests(), AUTHOR, store=store, max_age=DAY)
    assert [b.asin for b in result.books] == found
    assert result.from_store == tuple(found) and result.snapshot_at is not None
    assert not walk.called


async def test_an_author_discovery_404_deletes_the_snapshot_and_raises(store, monkeypatch):
    found = asins(2)
    monkeypatch.setattr(WALK, AsyncMock(return_value=(list(found), True)))
    await get_author_books(_hydrating_get(), AUTHOR, store=store)
    monkeypatch.setattr(WALK, AsyncMock(side_effect=NotFoundException("gone")))
    with pytest.raises(NotFoundException):
        await get_author_books(_hydrating_get(), AUTHOR, store=store)
    assert await _row(store, AUTHOR_BOOKS, AUTHOR) is None


async def test_an_author_outage_leaves_the_snapshot_alone(store, monkeypatch):
    found = asins(2)
    monkeypatch.setattr(WALK, AsyncMock(return_value=(list(found), True)))
    await get_author_books(_hydrating_get(), AUTHOR, store=store)
    before = await _row(store, AUTHOR_BOOKS, AUTHOR)
    monkeypatch.setattr(
        WALK, AsyncMock(side_effect=AudibleAPIException("down", upstream_status=503))
    )
    with pytest.raises(AudibleAPIException):
        await get_author_books(_hydrating_get(), AUTHOR, store=store)
    assert await _row(store, AUTHOR_BOOKS, AUTHOR) == before


async def test_an_incomplete_author_discovery_is_recorded_incomplete(store, monkeypatch):
    found = asins(2)
    monkeypatch.setattr(WALK, AsyncMock(return_value=(list(found), False)))
    await get_author_books(_hydrating_get(), AUTHOR, store=store)
    assert (await _row(store, AUTHOR_BOOKS, AUTHOR))["complete"] is False
    calls = []
    await get_author_books(_hydrating_get(calls=calls), AUTHOR, store=store, max_age=DAY)
    assert calls, "an incomplete author snapshot is never served"


# Section: stored list shape


async def test_a_stored_list_keeps_served_order_and_shapes_afterwards(store):
    found = asins(4)
    await get_series_books(_series_members(found), SERIES, store=store, sort="title", order="desc")
    row = await _row(store, SERIES_BOOKS, SERIES)
    assert row["book_asins"] == found, "recorded before sorting, in served order"
    plain = await get_series_books(_no_requests(), SERIES, store=store, max_age=DAY)
    assert [b.asin for b in plain.books] == found
    desc = await get_series_books(
        _no_requests(), SERIES, store=store, max_age=DAY, sort="title", order="desc"
    )
    assert [b.asin for b in desc.books] == found[::-1]


async def test_a_filter_on_a_stored_list_does_not_shrink_from_store(store):
    found = asins(3)
    await get_series_books(_series_members(found), SERIES, store=store)
    result = await get_series_books(
        _no_requests(), SERIES, store=store, max_age=DAY, filters={"language": "klingon"}
    )
    assert result.books == [] and result.from_store == tuple(found)
    assert result.complete is True


async def test_a_duplicate_entry_is_served_once_in_order(store, monkeypatch):
    found = asins(3)
    await get_series_books(_series_members(found), SERIES, store=store)
    row = await _row(store, SERIES_BOOKS, SERIES)
    row["book_asins"] = [found[1], found[0], found[1], found[2]]
    monkeypatch.setattr("libex_core.lookup._store.stored_walk", AsyncMock(return_value=row))
    result = await get_series_books(_no_requests(), SERIES, store=store, max_age=DAY)
    assert [b.asin for b in result.books] == [found[1], found[0], found[2]]


# Section: hostile rows planted raw


async def _planted(store, monkeypatch, found, **raw):
    await get_series_books(_series_members(found), SERIES, store=store)
    row = {**await _row(store, SERIES_BOOKS, SERIES), **raw}
    monkeypatch.setattr("libex_core.lookup._store.stored_walk", AsyncMock(return_value=row))
    calls = []
    result = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, max_age=DAY
    )
    return calls, result


BAD_ENTRIES = [
        lambda f: [f[0], "B0LOK000000"],
        lambda f: [f[0], "b0lok0000"],
        lambda f: [f[0], "B0LOK00001\n"],
        lambda f: [f[0], "ﬃ" * 3],
        lambda f: [f[0], "B" * 13],
        lambda f: [f[0], None],
        lambda f: [f[0], ["B0LOK00001"]],
        lambda f: [f[0], b"B0LOK00001"],
        lambda f: [*f, "x"],
        lambda f: ("B0LOK00000", "B0LOK00001"),
        lambda f: None,
        lambda f: 5,
    ]


@pytest.mark.parametrize("entries", BAD_ENTRIES)
async def test_one_bad_entry_spoils_the_whole_snapshot(store, monkeypatch, entries):
    found = asins(2)
    calls, result = await _planted(store, monkeypatch, found, book_asins=entries(found))
    assert calls and result.snapshot_at is None
    assert len(result.books) == 2, "the good entries are not served alone"


@pytest.mark.parametrize("entries", BAD_ENTRIES)
async def test_a_bad_entry_is_rejected_before_any_store_read_of_it(store, monkeypatch, entries):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    planted = entries(found)
    row = {**await _row(store, SERIES_BOOKS, SERIES), "book_asins": planted}
    monkeypatch.setattr("libex_core.lookup._store.stored_walk", AsyncMock(return_value=row))
    real, asked = _store.stored_books, []

    async def spy(store_, wanted, *args, **kwargs):
        asked.append(list(wanted))
        return await real(store_, wanted, *args, **kwargs)

    monkeypatch.setattr("libex_core.lookup._store.stored_books", spy)
    await get_series_books(_series_members(found), SERIES, store=store, max_age=DAY)
    assert all(set(w) <= set(found) for w in asked), "a planted entry reached the reader"


async def test_a_snapshot_over_the_cap_is_a_miss_not_truncated(store, monkeypatch):
    found = asins(2)
    many = [*found, *[f"B{n:09d}" for n in range(MAX_WALK_ASINS)]]
    calls, result = await _planted(store, monkeypatch, found, book_asins=many)
    assert calls and result.snapshot_at is None


async def test_a_snapshot_exactly_at_the_cap_is_not_over_it(store, monkeypatch):
    assert len(_walks._validated_asins([f"B{n:09d}" for n in range(MAX_WALK_ASINS)])) == MAX_WALK_ASINS


async def test_a_list_one_over_the_cap_is_rejected_whole_by_the_validator():
    with pytest.raises(_walks._Miss):
        _walks._validated_asins([f"B{n:09d}" for n in range(MAX_WALK_ASINS + 1)])


@pytest.mark.parametrize(
    "result_kw, from_store",
    [
        ({"complete": True, "store_write_failed": True}, []),
        ({"complete": True, "store_write_failed": False}, ["B0LOK00000"]),
        ({"complete": False, "store_write_failed": False}, []),
    ],
)
async def test_a_walk_is_recorded_complete_only_when_the_store_holds_all_of_it(
    monkeypatch, result_kw, from_store
):
    seen = {}

    async def persist(store, kind, asin, region, book_asins, **kw):
        seen.update(kw)
        return True

    monkeypatch.setattr("libex_core.lookup._store.persist_walk", persist)
    result = SimpleNamespace(incomplete_reasons=(), **result_kw)
    hydration = SimpleNamespace(books=[{"asin": "B0LOK00000"}], from_store=from_store)
    await _walks.record_walk(
        object(), SERIES_BOOKS, SERIES, "us", result, hydration, datetime.now(timezone.utc)
    )
    assert seen["complete"] is False


async def test_a_lowercase_entry_of_valid_shape_is_read_back_uppercased():
    assert _walks._validated_asins(["b0lok00000", "B0LOK00000"]) == ["B0LOK00000"]


@pytest.mark.parametrize(
    "exc",
    [RecursionError("deep"), ValueError("bad json"), TypeError("t"), RuntimeError("drv")],
)
async def test_any_error_reading_a_row_is_a_miss_and_logs_only_the_type(
    store, monkeypatch, exc, caplog
):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    monkeypatch.setattr(
        "libex_core.lookup._store.stored_walk", AsyncMock(side_effect=exc)
    )
    calls = []
    with caplog.at_level("INFO", logger="libex"):
        result = await get_series_books(
            _series_members(found, calls=calls), SERIES, store=store, max_age=DAY
        )
    assert calls and result.snapshot_at is None
    assert "deep" not in caplog.text and "bad json" not in caplog.text


async def test_a_failed_stored_book_read_is_a_miss(store, monkeypatch):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    real, seen = _store.stored_books, []

    async def first_fails(*args, **kwargs):
        seen.append(1)
        if len(seen) == 1:
            raise RuntimeError("x")
        return await real(*args, **kwargs)

    monkeypatch.setattr("libex_core.lookup._store.stored_books", first_fails)
    calls = []
    result = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, max_age=DAY
    )
    assert calls and result.snapshot_at is None


async def test_a_hit_logs_counts_and_never_row_contents(store, caplog):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    with caplog.at_level("INFO", logger="libex"):
        await get_series_books(_no_requests(), SERIES, store=store, max_age=DAY)
    hit = [r for r in caplog.records if r.getMessage() == "Answered from a stored list"]
    assert len(hit) == 1 and hit[0].book_num == 2 and hit[0].kind == SERIES_BOOKS
    assert found[0] not in caplog.text


# Section: freshness


async def test_a_slightly_future_confirmed_at_is_within_skew(store):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    await _patch_row(store, confirmed_at=datetime.now(timezone.utc) + timedelta(seconds=60))
    result = await get_series_books(_no_requests(), SERIES, store=store, max_age=DAY)
    assert result.snapshot_at is not None


async def test_a_confirmed_at_past_the_skew_is_never_served(store):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    await _patch_row(store, confirmed_at=datetime.now(timezone.utc) + timedelta(minutes=30))
    calls = []
    result = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, max_age=timedelta.max
    )
    assert calls and result.snapshot_at is None


async def test_a_snapshot_just_past_max_age_goes_live(store):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    await _patch_row(store, confirmed_at=datetime.now(timezone.utc) - timedelta(minutes=10))
    calls = []
    result = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, max_age=timedelta(minutes=5)
    )
    assert calls and result.snapshot_at is None


async def test_a_future_dated_row_is_overwritten_by_the_next_live_walk_and_then_served(store):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store)
    future = datetime.now(timezone.utc) + timedelta(days=30)
    await _patch_row(store, confirmed_at=future)
    calls = []
    live = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, max_age=DAY
    )
    assert calls and live.snapshot_at is None, "future-dated is never served"
    confirmed = (await _row(store, SERIES_BOOKS, SERIES))["confirmed_at"]
    assert confirmed < datetime.now(timezone.utc) + timedelta(minutes=1), "overwritten"
    again = await get_series_books(_no_requests(), SERIES, store=store, max_age=DAY)
    assert again.snapshot_at is not None


# Section: region scoping


async def test_a_snapshot_naming_books_stored_only_for_another_region_is_a_miss(store):
    found = asins(2)
    await get_series_books(_series_members(found), SERIES, store=store, region="us")
    row = await _row(store, SERIES_BOOKS, SERIES)
    async with store.write() as session:
        await write_walk_result(
            session, kind=SERIES_BOOKS, asin=SERIES, region="uk",
            book_asins=row["book_asins"], complete=True, incomplete_reasons=[],
            at=datetime.now(timezone.utc),
        )
    calls = []
    result = await get_series_books(
        _series_members(found, calls=calls), SERIES, store=store, region="uk", max_age=DAY
    )
    assert calls and result.snapshot_at is None, "the us books are not the uk's"


# Section: a stored list is the same answer as the live one

# The fields that legitimately differ between a live list and one answered
# from a stored walk; every other BookList field must be equal.
_DOCUMENTED_DIFFERENCES = {"snapshot_at", "from_store", "explicit_nulls"}


def assert_same_answer(live, stored):
    assert [b.model_dump() for b in stored.books] == [b.model_dump() for b in live.books]
    assert live.books, "an empty list would prove nothing"
    names = {f.name for f in fields(live)}
    assert _DOCUMENTED_DIFFERENCES <= names
    for name in names - _DOCUMENTED_DIFFERENCES - {"books"}:
        assert getattr(stored, name) == getattr(live, name), name


async def test_a_stored_series_list_is_the_live_list_field_for_field(store):
    found = asins(4)
    live = await get_series_books(_series_members(found), SERIES, store=store)
    stored = await get_series_books(_no_requests(), SERIES, store=store, max_age=DAY)
    assert stored.snapshot_at is not None
    assert_same_answer(live, stored)


async def test_a_stored_series_list_is_the_live_list_when_shaped(store):
    found = asins(4)
    live = await get_series_books(
        _series_members(found), SERIES, store=store, sort="title", order="desc"
    )
    stored = await get_series_books(
        _no_requests(), SERIES, store=store, max_age=DAY, sort="title", order="desc"
    )
    assert_same_answer(live, stored)


async def test_a_stored_author_list_is_the_live_list_field_for_field(store, monkeypatch):
    found = asins(4)
    monkeypatch.setattr(WALK, AsyncMock(return_value=(list(found), True)))
    live = await get_author_books(_hydrating_get(), AUTHOR, store=store)
    stored = await get_author_books(_no_requests(), AUTHOR, store=store, max_age=DAY)
    assert stored.snapshot_at is not None
    assert_same_answer(live, stored)
