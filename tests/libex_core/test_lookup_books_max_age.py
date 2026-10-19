"""
max_age on get_book and get_books: a book the store holds for the region asked
and that Audible confirmed recently enough is served with no request, and
anything else is asked for live. A real SQLite store and a stand-in `get`
that counts its requests; nothing touches a network.
"""

# Standard library
import logging
from datetime import datetime, timedelta, timezone

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import update

# Local
from libex_core.exceptions import AudibleAPIException, NotFoundException
from libex_core.lookup import (
    get_book,
    get_book_with_nulls,
    get_books,
    get_books_with_nulls,
)
from libex_core.lookup import _store
from libex_core.lookup.books import _hydrate
from libex_core.storage.models import Book
from libex_core.storage.store import LocalStore
from tests.libex_core._lookup_support import not_found_get, outage_get, product
from tests.libex_core.test_lookup_store import batch_get

DAY = timedelta(days=1)
A, B, C = "B0MAX00001", "B0MAX00002", "B0MAX00003"
REGIONS = ["us", "uk", "ca", "au", "de", "fr", "it", "es", "jp", "in", "br"]


@pytest_asyncio.fixture
async def store(tmp_path):
    local = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    await local.upgrade()
    await local.open()
    yield local
    await local.close()


def counting(**by_asin):
    """A get that answers like batch_get and records each request it is sent."""
    inner = batch_get(**{a: product(a) for a in by_asin})
    sent = []

    async def get(region, path, params=None, extra_headers=None):
        sent.append((region, path, (params or {}).get("asins")))
        return await inner(region, path, params, extra_headers)

    get.sent = sent
    return get


async def stamp(store, asin, at, region="us"):
    async with store.write() as session:
        await session.execute(
            update(Book).where(Book.asin == asin, Book.region == region).values(confirmed_at=at)
        )


async def seed(store, *asins, region="us"):
    await get_books(counting(**{a: 1 for a in asins}), list(asins), region=region, store=store)


def requested(get):
    return sorted(a for _, _, joined in get.sent for a in (joined or "").split(",") if a)


async def test_a_fresh_book_is_served_with_no_request(store):
    await seed(store, A)
    get = counting(**{A: 1})
    book = await get_book(get, A, store=store, max_age=DAY)
    assert book.asin == A and get.sent == []


async def test_without_max_age_the_book_is_always_asked_for(store):
    await seed(store, A)
    get = counting(**{A: 1})
    await get_book(get, A, store=store)
    assert get.sent


async def test_a_stale_book_goes_live_and_is_confirmed_again(store):
    await seed(store, A)
    await stamp(store, A, datetime.now(timezone.utc) - 3 * DAY)
    get = counting(**{A: 1})
    await get_book(get, A, store=store, max_age=DAY)
    assert get.sent
    again = counting(**{A: 1})
    await get_book(again, A, store=store, max_age=DAY)
    assert again.sent == []


async def test_a_never_confirmed_book_is_never_fresh(store):
    await seed(store, A)
    await stamp(store, A, None)
    get = counting(**{A: 1})
    await get_book(get, A, store=store, max_age=timedelta.max)
    assert get.sent


async def test_a_future_dated_book_is_not_fresh(store):
    await seed(store, A)
    await stamp(store, A, datetime.now(timezone.utc) + 30 * DAY)
    get = counting(**{A: 1})
    await get_book(get, A, store=store, max_age=timedelta.max)
    assert get.sent


async def test_a_stamp_inside_the_allowed_skew_is_fresh(store):
    await seed(store, A)
    await stamp(store, A, datetime.now(timezone.utc) + timedelta(seconds=5))
    get = counting(**{A: 1})
    await get_book(get, A, store=store, max_age=DAY)
    assert get.sent == []


@pytest.mark.parametrize("region", REGIONS)
async def test_a_book_stored_for_another_region_is_never_served(store, region):
    other = "uk" if region != "uk" else "us"
    await seed(store, A, region=other)
    get = counting(**{A: 1})
    await get_book(get, A, region=region, store=store, max_age=timedelta.max)
    assert get.sent and get.sent[0][0] == region


async def test_a_mixed_bulk_asks_once_for_only_the_stale_and_missing(store):
    await seed(store, A, B)
    await stamp(store, B, datetime.now(timezone.utc) - 3 * DAY)
    get = counting(**{A: 1, B: 1, C: 1})
    got = await get_books_with_nulls(get, [C, B, A], store=store, max_age=DAY)
    assert len(get.sent) == 1 and requested(get) == [B, C]
    assert [b.asin for b in got.response.books] == [C, B, A]
    assert got.response.notFound == got.response.notFetched == []
    assert set(got.explicit_nulls) == {B, C}


async def test_a_fresh_book_sits_between_two_stale_ones_in_request_order(store):
    await seed(store, A, B, C)
    for stale in (A, C):
        await stamp(store, stale, datetime.now(timezone.utc) - 3 * DAY)
    get = counting(**{A: 1, B: 1, C: 1})
    got = await get_books(get, [A, B, C], store=store, max_age=DAY)
    assert requested(get) == [A, C]
    assert [b.asin for b in got.books] == [A, B, C]


async def test_the_fresh_books_are_listed_as_from_store_on_every_path(store):
    await seed(store, A, B)
    await stamp(store, B, datetime.now(timezone.utc) - 3 * DAY)
    single = await _hydrate(counting(**{A: 1}), [A], "us", store, DAY)
    assert single.from_store == [A]
    bulk = await _hydrate(counting(**{A: 1, B: 1}), [B, A], "us", store, DAY)
    assert bulk.from_store == [A] and [b["asin"] for b in bulk.books] == [B, A]
    outage = await _hydrate(outage_get, [A, B, C], "us", store, DAY)
    assert A in outage.from_store and outage.not_fetched == [C]


async def test_a_fully_fresh_bulk_makes_no_request_and_has_no_nulls(store):
    await seed(store, A, B)
    get = counting(**{A: 1, B: 1})
    got = await get_books_with_nulls(get, [B, A, A.lower()], store=store, max_age=DAY)
    assert get.sent == []
    assert [b.asin for b in got.response.books] == [B, A]
    assert got.explicit_nulls == {}


async def test_a_missing_asin_is_not_found_and_a_stale_one_is_not_answered_over_it(store):
    await seed(store, A, B)
    await stamp(store, B, datetime.now(timezone.utc) - 3 * DAY)
    get = batch_get(**{A: product(A)})
    got = await get_books(get, [A, B, C], store=store, max_age=DAY)
    assert [b.asin for b in got.books] == [A]
    assert got.notFound == [B, C]


async def test_a_book_audible_confirms_absent_is_never_served_from_the_store(store):
    await seed(store, A)
    await stamp(store, A, datetime.now(timezone.utc) - 3 * DAY)
    with pytest.raises(NotFoundException):
        await get_book(not_found_get, A, store=store, max_age=DAY)


async def test_an_outage_serves_the_fresh_books_and_reports_the_rest(store):
    await seed(store, A)
    got = await get_books(outage_get, [A, C], store=store, max_age=DAY)
    assert [b.asin for b in got.books] == [A]
    assert got.notFetched == [C] and got.notFound == []


async def test_an_outage_with_nothing_fresh_falls_back_as_before(store):
    await seed(store, A)
    await stamp(store, A, datetime.now(timezone.utc) - 3 * DAY)
    got = await get_books(outage_get, [A, C], store=store, max_age=DAY)
    assert [b.asin for b in got.books] == [A] and got.notFetched == [C]
    with pytest.raises(AudibleAPIException):
        await get_books(outage_get, [C], store=store, max_age=DAY)


async def test_a_served_single_book_has_unknown_nulls(store):
    await seed(store, A)
    got = await get_book_with_nulls(counting(**{A: 1}), A, store=store, max_age=DAY)
    assert got.explicit_nulls is None


@pytest.mark.parametrize("max_age", [0, 1.5, "1d", timedelta(0), -DAY])
async def test_a_bad_max_age_is_a_value_error_before_anything_is_sent(store, max_age):
    get = counting(**{A: 1})
    with pytest.raises(ValueError):
        await get_book(get, A, store=store, max_age=max_age)
    with pytest.raises(ValueError):
        await get_books(get, [A], store=store, max_age=max_age)
    assert get.sent == []


async def test_max_age_without_a_store_is_a_value_error():
    get = counting(**{A: 1})
    with pytest.raises(ValueError):
        await get_book(get, A, max_age=DAY)
    with pytest.raises(ValueError):
        await get_books(get, [A], max_age=DAY)
    assert get.sent == []


async def test_a_failed_read_is_a_miss_and_logs_no_values(store, monkeypatch, caplog):
    await seed(store, A)

    def broken(*args, **kwargs):
        raise RuntimeError(f"secret {A}")

    monkeypatch.setattr(store, "session", broken)
    monkeypatch.setattr(_store, "check", _noop)
    get = counting(**{A: 1})
    with caplog.at_level(logging.DEBUG, logger="libex"):
        await get_books(get, [A], store=store, max_age=DAY)
    assert get.sent
    assert "Store read failed" in caplog.text and "secret" not in caplog.text


async def _noop(store):
    return None
