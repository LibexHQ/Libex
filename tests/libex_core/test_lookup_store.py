"""
libex_core.lookup with a LocalStore: what a lookup keeps, serves and falls
back to. Every test runs against a real SQLite store (a file, upgraded by the
package's own migrations) and a stand-in `get`, so the writer, the merge rules
and the readers are the real ones and nothing touches a network.
"""

# Standard library
import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

# Third party
import pytest
import pytest_asyncio

# Local
from libex_core.exceptions import AudibleAPIException, NotFoundException
from libex_core.lookup import (
    get_author,
    get_author_books,
    get_book,
    get_books,
    get_chapters,
    get_series,
    get_series_books,
    new_releases,
    quick_search,
    search,
    search_authors,
    search_series,
)
from libex_core.lookup.books import hydrate_books
from libex_core.models import BookResponse
from libex_core.storage.read import books as read_books
from libex_core.storage.read import people as read_people
from libex_core.storage.read import series as read_series
from libex_core.storage.store import LocalStore, StoreClosed
from tests.libex_core._lookup_support import (
    AUTHOR,
    AUTHOR_NAME,
    BOOKS,
    SERIES,
    fake_get,
    not_found_get,
    outage_get,
    product,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ASIN = "B0SCR00000"
OTHER = "B0SCR00001"
LONG_SUMMARY = "A long and detailed summary of the book, well past the short one."
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


def batch_get(**by_asin):
    """A get answering batch lookups with the product given for each ASIN
    (a missing one is a hollow stub), and the chapter, author and series
    endpoints from the shared stand-in."""
    async def get(region, path, params=None, extra_headers=None):
        tail = path.rsplit("/", 1)[-1]
        if path.startswith("/1.0/catalog/products/") and not tail.startswith("B0SERIES"):
            return {"product": by_asin[tail]} if tail in by_asin else {}
        if params and "asins" in params and "contributors/" not in path:
            return {"products": [by_asin.get(a, {"asin": a}) for a in params["asins"].split(",")]}
        return await fake_get(region, path, params, extra_headers)
    return get


async def stored_book(store, asin):
    async with store.session() as session:
        return await read_books.get_book(session, asin)


async def stored_asins(store):
    async with store.session() as session:
        return sorted(row["asin"] for row in await read_books.get_books(session, list(BOOKS)))


# Section: books, written through and served merged

async def test_a_book_is_written_through(store):
    book = await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)

    row = await stored_book(store, ASIN)
    assert row["title"] == f"Title {ASIN}" == book.title
    assert [a["name"] for a in row["authors"]] == [AUTHOR_NAME]


async def test_without_a_store_nothing_is_written_and_the_answer_is_the_same(store):
    live = await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN)
    kept = await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)

    # What the store adds is the row ids, which Audible does not know.
    def comparable(book):
        data = book.model_dump(exclude={"updatedAt"})
        data["authors"] = [{**a, "id": None, "updatedAt": None} for a in data["authors"]]
        return data

    assert comparable(live) == comparable(kept)


async def test_a_thinner_answer_is_served_merged_and_never_shrinks_the_row(store):
    rich = product(ASIN, subtitle="A Subtitle", publisher_summary=LONG_SUMMARY)
    thin = product(ASIN, publisher_summary="Short.")
    await get_book(batch_get(**{ASIN: rich}), ASIN, store=store)

    served = await get_book(batch_get(**{ASIN: thin}), ASIN, store=store)

    assert served.subtitle == "A Subtitle"
    assert served.summary == LONG_SUMMARY
    row = await stored_book(store, ASIN)
    assert (row["subtitle"], row["summary"]) == ("A Subtitle", LONG_SUMMARY)


async def test_a_richer_answer_is_served_merged_and_stored(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)

    served = await get_book(
        batch_get(**{ASIN: product(ASIN, subtitle="Now Known")}), ASIN, store=store
    )

    assert served.subtitle == "Now Known"
    assert (await stored_book(store, ASIN))["subtitle"] == "Now Known"


async def test_a_bulk_lookup_serves_each_book_merged(store):
    await get_book(batch_get(**{ASIN: product(ASIN, subtitle="Kept")}), ASIN, store=store)

    got = await get_books(
        batch_get(**{ASIN: product(ASIN), OTHER: product(OTHER)}), [ASIN, OTHER], store=store
    )

    assert {b.asin: b.subtitle for b in got.books} == {ASIN: "Kept", OTHER: None}
    assert (got.notFound, got.notFetched) == ([], [])
    assert sorted(await stored_asins(store)) == [ASIN, OTHER]


# Section: books, an outage answered from the store

async def test_an_outage_is_answered_from_the_stored_book(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)

    book = await get_book(outage_get, ASIN, store=store)

    assert book.asin == ASIN and book.title == f"Title {ASIN}"


async def test_an_outage_with_nothing_stored_is_still_an_outage(store):
    with pytest.raises(AudibleAPIException):
        await get_book(outage_get, ASIN, store=store)


async def test_a_bulk_outage_serves_what_is_stored_and_reports_the_rest(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)

    got = await get_books(outage_get, [ASIN, OTHER], store=store)

    assert [b.asin for b in got.books] == [ASIN]
    assert got.notFetched == [OTHER]
    assert got.notFound == []


async def test_a_failed_chunk_is_answered_from_the_store_beside_the_chunks_that_answered(store):
    chunk_one = [f"B0LOK{i:05d}" for i in range(50)]
    chunk_two = [f"B0LOK{i:05d}" for i in range(50, 55)]
    await get_books(
        batch_get(**{a: product(a) for a in chunk_one + chunk_two}), chunk_one + chunk_two,
        store=store,
    )

    async def get(region, path, params=None, extra_headers=None):
        wanted = params["asins"].split(",")
        if wanted[0] == chunk_two[0]:
            raise AudibleAPIException("boom", upstream_status=503)
        return {"products": [product(a) for a in wanted]}

    hydration = await hydrate_books(get, chunk_one + chunk_two, "us", store=store)

    assert sorted(b["asin"] for b in hydration.books) == sorted(chunk_one + chunk_two)
    assert sorted(hydration.from_store) == sorted(chunk_two)
    assert hydration.not_fetched == []


async def test_a_confirmed_absence_is_never_answered_from_the_store(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)

    with pytest.raises(NotFoundException):
        await get_book(not_found_get, ASIN, store=store)
    with pytest.raises(NotFoundException):
        await get_book(batch_get(), ASIN, store=store)
    got = await get_books(batch_get(), [ASIN], store=store)
    assert got.books == [] and got.notFound == [ASIN]


async def test_an_outage_elsewhere_does_not_overrule_an_absence_a_chunk_confirmed(store):
    confirmed_absent = [f"B0LOK{i:05d}" for i in range(50)]
    unreachable = f"B0LOK{50:05d}"
    stored = [confirmed_absent[0], unreachable]
    await get_books(batch_get(**{a: product(a) for a in stored}), stored, store=store)

    async def get(region, path, params=None, extra_headers=None):
        wanted = params["asins"].split(",")
        if wanted[0] == unreachable:
            raise AudibleAPIException("boom", upstream_status=503)
        return {"products": [{"asin": a} for a in wanted]}

    got = await get_books(get, confirmed_absent + [unreachable], store=store)

    assert [b.asin for b in got.books] == [unreachable]
    assert confirmed_absent[0] in got.notFound
    assert got.notFetched == []


# Section: write failures and a store that cannot be used

async def test_a_failed_write_is_logged_returned_live_and_reported(store, monkeypatch, caplog):
    from libex_core.storage import write

    async def broken(session, books, **kwargs):
        raise RuntimeError(f"secret detail for {books[0]['asin']}")

    monkeypatch.setattr(write, "write_books", broken)
    caplog.set_level(logging.WARNING, logger="libex")

    hydration = await hydrate_books(batch_get(**{ASIN: product(ASIN)}), [ASIN], "us", store=store)

    assert [b["asin"] for b in hydration.books] == [ASIN]
    assert hydration.store_write_failed is True
    assert await stored_asins(store) == []
    failure = [r for r in caplog.records if r.getMessage().startswith("Store write failed")]
    assert failure and failure[0].error_type == "RuntimeError"
    assert "secret detail" not in caplog.text and ASIN not in caplog.text


async def test_a_closed_store_is_the_callers_error(store):
    await store.close()
    with pytest.raises(StoreClosed):
        await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    with pytest.raises(StoreClosed):
        await get_chapters(fake_get, ASIN, store=store)


# Section: chapters

CHAPTERS = {"content_metadata": {"chapter_info": {
    "runtime_length_ms": 3000,
    "chapters": [
        {"length_ms": 1000, "start_offset_ms": 0, "title": "One"},
        {"length_ms": 2000, "start_offset_ms": 1000, "title": "Two"},
    ],
}}}
# A listing that answered but names no chapters: the thinnest thing a 200 can say.
EMPTY_LISTING = {"content_metadata": {"chapter_info": {"runtime_length_ms": 3000, "chapters": []}}}


def chapter_get(payload):
    async def get(region, path, params=None, extra_headers=None):
        return payload
    return get


async def test_chapters_of_a_book_the_store_lacks_are_returned_live_and_not_stored(store):
    chapters = await get_chapters(chapter_get(CHAPTERS), ASIN, store=store)

    assert len(chapters.chapters) == 2
    async with store.session() as session:
        assert await read_books.get_track(session, ASIN) is None


async def test_chapters_are_written_through_and_a_listing_with_none_never_replaces_them(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    first = await get_chapters(chapter_get(CHAPTERS), ASIN, store=store)
    assert len(first.chapters) == 2

    served = await get_chapters(chapter_get(EMPTY_LISTING), ASIN, store=store)

    assert [c.title for c in served.chapters] == ["One", "Two"]
    assert served.runtimeLengthMs == 3000
    async with store.session() as session:
        assert len((await read_books.get_track(session, ASIN))["chapters"]) == 2


async def test_chapters_outage_is_answered_from_the_store_and_a_404_is_not(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    await get_chapters(chapter_get(CHAPTERS), ASIN, store=store)

    assert len((await get_chapters(outage_get, ASIN, store=store)).chapters) == 2
    with pytest.raises(NotFoundException):
        await get_chapters(not_found_get, ASIN, store=store)
    with pytest.raises(NotFoundException):
        await get_chapters(chapter_get({"content_metadata": {}}), ASIN, store=store)
    with pytest.raises(AudibleAPIException):
        await get_chapters(outage_get, OTHER, store=store)


# Section: series

async def test_a_series_is_written_through_and_an_outage_is_answered_from_it(store):
    live = await get_series(fake_get, SERIES, store=store)
    async with store.session() as session:
        assert (await read_series.get_series(session, SERIES))["name"] == live.name

    assert (await get_series(outage_get, SERIES, store=store)).name == live.name
    with pytest.raises(NotFoundException):
        await get_series(not_found_get, SERIES, store=store)
    with pytest.raises(AudibleAPIException):
        await get_series(outage_get, "B0SERIES09", store=store)


async def test_series_books_fall_back_to_the_stored_members_in_series_order(store):
    members = {
        a: product(a, relationships=[{**SERIES_RELATION, "sequence": str(i + 1)}])
        for i, a in enumerate(BOOKS)
    }
    got = await get_series_books(batch_get(**members), SERIES, store=store)
    assert [b.asin for b in got.books] == list(BOOKS)
    assert got.complete is True
    assert got.incomplete_reasons == ()

    async def member_list_down(region, path, params=None, extra_headers=None):
        raise AudibleAPIException("boom", upstream_status=503)

    again = await get_series_books(member_list_down, SERIES, store=store)
    assert [b.asin for b in again.books] == list(BOOKS)
    # The stored members stand in for a member list Audible could not give, so
    # the membership is unconfirmed even though every stored book came back.
    assert again.complete is False
    assert again.incomplete_reasons == ("discovery-incomplete",)
    with pytest.raises(AudibleAPIException):
        await get_series_books(member_list_down, "B0SERIES09", store=store)


async def test_series_search_adds_the_stored_series_after_audibles(store):
    await get_series(fake_get, SERIES, store=store)
    async with store.write() as session:
        from libex_core.storage import write
        await write.write_series_profile(
            session, {"asin": "B0STORED01", "name": "The Series Revisited", "region": "us"}
        )

    found = await search_series(fake_get, "The Series", store=store)

    assert [s.asin for s in found][-1] == "B0STORED01"
    assert [s.asin for s in found].count(SERIES) == 1


# Section: authors

async def test_an_author_is_written_through_and_an_outage_is_answered_from_it(store):
    live = await get_author(fake_get, AUTHOR, store=store)
    async with store.session() as session:
        assert (await read_people.get_author(session, AUTHOR, "us"))["name"] == AUTHOR_NAME

    assert (await get_author(outage_get, AUTHOR, store=store)).name == live.name
    assert (await get_author(outage_get, AUTHOR, store=store)).description == live.description
    with pytest.raises(NotFoundException):
        await get_author(not_found_get, AUTHOR, store=store)
    with pytest.raises(AudibleAPIException):
        await get_author(outage_get, "B0AUTHOR99", store=store)


async def test_an_author_is_kept_per_region(store):
    await get_author(fake_get, AUTHOR, region="us", store=store)
    with pytest.raises(AudibleAPIException):
        await get_author(outage_get, AUTHOR, region="de", store=store)


async def test_author_search_resolves_through_the_store(store):
    await get_author(fake_get, AUTHOR, store=store)

    async def suggestions_then_down(region, path, params=None, extra_headers=None):
        if "searchsuggestions" in path:
            return await fake_get(region, path, params, extra_headers)
        raise AudibleAPIException("boom", upstream_status=503)

    found = await search_authors(suggestions_then_down, "Jane", store=store)

    assert [a.asin for a in found] == [AUTHOR]


async def test_author_books_union_in_what_the_store_holds(store):
    held = "B0HELD0001"
    await get_book(batch_get(**{held: product(held)}), held, store=store)
    live = {**BOOKS, held: product(held)}

    without = await get_author_books(batch_get(**BOOKS), AUTHOR)
    with_store = await get_author_books(batch_get(**live), AUTHOR, store=store)

    assert held not in {b.asin for b in without.books}
    assert {b.asin for b in with_store.books} == set(BOOKS) | {held}
    assert set(await stored_asins(store)) == set(BOOKS)


async def test_author_books_from_a_walk_that_finds_nothing_is_still_the_outcome_it_was(store):
    async def nothing(region, path, params=None, extra_headers=None):
        return await fake_get(region, path, params, extra_headers) if "contributors" in path \
            else {"products": [], "total_results": 0, "nothing": True}

    with pytest.raises(NotFoundException):
        await get_author_books(nothing, AUTHOR, store=store)


async def test_author_books_write_failure_is_reported_on_the_result(store, monkeypatch):
    from libex_core.storage import write

    async def broken(session, books, **kwargs):
        raise RuntimeError("x")

    monkeypatch.setattr(write, "write_books", broken)
    monkeypatch.setattr(
        "libex_core.lookup.author_books._walk_author_books",
        AsyncMock(return_value=([ASIN], True)),
    )

    result = await get_author_books(batch_get(**{ASIN: product(ASIN)}), AUTHOR, store=store)

    assert [b.asin for b in result.books] == [ASIN]
    assert result.store_write_failed is True


# Section: search and releases

def catalog_get(*products):
    async def get(region, path, params=None, extra_headers=None):
        return {"products": list(products)}
    return get


async def test_search_results_are_written_through_and_served_merged(store):
    await get_book(batch_get(**{ASIN: product(ASIN, subtitle="Kept")}), ASIN, store=store)

    found = await search(catalog_get(product(ASIN), product(OTHER)), title="any", store=store)

    by_asin = {b.asin: b for b in found}
    assert by_asin[ASIN].subtitle == "Kept"
    assert set(await stored_asins(store)) == {ASIN, OTHER} == set(by_asin)


async def test_a_search_audible_cannot_answer_is_not_answered_from_the_store(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)

    with pytest.raises(AudibleAPIException):
        await search(outage_get, title="Title", store=store)


async def test_quick_search_hydration_falls_back_to_stored_books(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)

    async def suggests_then_down(region, path, params=None, extra_headers=None):
        if "searchsuggestions" in path:
            return {"model": {"items": [
                {"view": {"template": "AsinRow"}, "model": {"product_metadata": {"asin": ASIN}}}
            ]}}
        raise AudibleAPIException("boom", upstream_status=503)

    found = await quick_search(suggests_then_down, "Title", store=store)

    assert [b.asin for b in found] == [ASIN]


async def test_a_compound_quick_search_the_catalog_cannot_answer_tries_the_store(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)

    async def no_suggestions_then_down(region, path, params=None, extra_headers=None):
        if "searchsuggestions" in path:
            return {"model": {"items": []}}
        raise AudibleAPIException("boom", upstream_status=503)

    keywords = f"{AUTHOR_NAME} - Title {ASIN}"
    found = await quick_search(no_suggestions_then_down, keywords, store=store)
    assert [b.asin for b in found] == [ASIN]

    with pytest.raises(AudibleAPIException):
        await quick_search(no_suggestions_then_down, f"{AUTHOR_NAME} - No Such Title", store=store)
    with pytest.raises(AudibleAPIException):
        await quick_search(no_suggestions_then_down, keywords)


async def test_new_releases_are_written_through_and_an_outage_stays_an_outage(store):
    found = await new_releases(fake_get, 60, store=store)

    assert found
    assert set(await stored_asins(store)) >= {b.asin for b in found}
    with pytest.raises(AudibleAPIException):
        await new_releases(outage_get, 60, store=store)


# Section: no store

NO_STORE_SCRIPT = """
import asyncio, sys
import libex_core.lookup as lookup

async def get(region, path, params=None, extra_headers=None):
    return {"product": {"asin": "B0SCR00000", "title": "T",
            "publication_datetime": "2020-01-01T00:00:00Z", "release_date": "2020-01-01"}}

book = asyncio.run(lookup.get_book(get, "B0SCR00000"))
assert book.title == "T"
loaded = sorted(m for m in sys.modules if m.split(".")[0] in ("sqlalchemy", "aiosqlite", "asyncpg"))
assert not loaded, loaded
assert "libex_core.storage.store" not in sys.modules
"""


def test_without_a_store_the_lookups_load_and_run_without_the_storage_libraries():
    done = subprocess.run(
        [sys.executable, "-c", NO_STORE_SCRIPT],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert done.returncode == 0, done.stderr


STORE_ONLY_READS = {
    "chapters_confirmed_at",
    "stored_author",
    "stored_book",
    "stored_books",
    "stored_chapters",
    "stored_series",
}


def test_every_lookup_takes_a_keyword_only_store_that_defaults_to_none():
    import inspect

    import libex_core.lookup as lookup

    for name in lookup.__all__:
        fn = getattr(lookup, name)
        if not inspect.iscoroutinefunction(fn) or name in STORE_ONLY_READS:
            continue
        param = inspect.signature(fn).parameters.get("store")
        assert param is not None, name
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert param.default is None, name


async def test_concurrent_lookups_write_without_losing_a_book(store):
    asins = [f"B0CON{i:05d}" for i in range(6)]
    get = batch_get(**{a: product(a) for a in asins})

    await asyncio.gather(*(get_book(get, a, store=store) for a in asins))

    async with store.session() as session:
        assert len(await read_books.get_books(session, asins)) == len(asins)
    assert BookResponse
