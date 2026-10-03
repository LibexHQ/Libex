"""
libex_core.lookup with a LocalStore while Audible is unavailable: every answer
the store gives is logged once under one message, is limited to the asked
region, and keeps the order the caller asked for. Runs against a real SQLite
store (a file, upgraded by the package's own migrations) and a stand-in `get`.
"""

# Standard library
import logging

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import text

# Local
from libex_core.exceptions import AudibleAPIException
from libex_core.lookup import (
    get_author,
    get_author_books,
    get_book,
    get_books,
    get_chapters,
    get_series,
    get_series_books,
    quick_search,
)
from libex_core.lookup import _store as store_module
from libex_core.lookup.books import hydrate_books
from libex_core.storage.read import books as read_books
from libex_core.storage.store import LocalStore
from tests.libex_core._lookup_support import (
    AUTHOR,
    AUTHOR_NAME,
    BOOKS,
    SERIES,
    asins,
    fake_get,
    outage_get,
    product,
)
from tests.libex_core.test_lookup_store import (
    ASIN,
    CHAPTERS,
    OTHER,
    SERIES_RELATION,
    batch_get,
    chapter_get,
)

SHARED = "Answered from the store while Audible was unavailable"
STANDARD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


@pytest_asyncio.fixture
async def store(tmp_path):
    local = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    await local.upgrade()
    await local.open()
    yield local
    await local.close()


def served_records(caplog):
    return [r for r in caplog.records if r.getMessage() == SHARED]


def fields_of(record):
    return {k: v for k, v in record.__dict__.items() if k not in STANDARD_FIELDS}


def one_served(caplog):
    records = served_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    return fields_of(records[0])


def chunk_failing(first_asin):
    """A get whose bulk request fails for the chunk starting at first_asin and
    answers every other chunk, reversed, like Audible is free to."""
    async def get(region, path, params=None, extra_headers=None):
        wanted = params["asins"].split(",")
        if wanted[0] == first_asin:
            raise AudibleAPIException("boom", upstream_status=503)
        return {"products": [product(a) for a in reversed(wanted)]}
    return get


async def member_list_down(region, path, params=None, extra_headers=None):
    raise AudibleAPIException("boom", upstream_status=503)


# Section: the shared warning, one site at a time

async def test_a_series_answered_from_the_store_is_logged(store, caplog):
    await get_series(fake_get, SERIES, store=store)
    caplog.set_level(logging.WARNING, logger="libex")

    await get_series(outage_get, SERIES, store=store)

    assert one_served(caplog) == {"what": "series", "region": "us", "series_asin": SERIES}


async def test_series_books_answered_from_the_stored_members_are_logged(store, caplog):
    members = {
        a: product(a, relationships=[{**SERIES_RELATION, "sequence": str(i + 1)}])
        for i, a in enumerate(BOOKS)
    }
    await get_series_books(batch_get(**members), SERIES, store=store)
    caplog.set_level(logging.WARNING, logger="libex")

    await get_series_books(member_list_down, SERIES, store=store)

    assert one_served(caplog) == {
        "what": "series books", "region": "us", "series_asin": SERIES,
        "stored_num": len(BOOKS),
    }


async def test_an_author_answered_from_the_store_is_logged(store, caplog):
    await get_author(fake_get, AUTHOR, store=store)
    caplog.set_level(logging.WARNING, logger="libex")

    await get_author(outage_get, AUTHOR, store=store)

    assert one_served(caplog) == {"what": "author", "region": "us", "author_asin": AUTHOR}


async def test_chapters_answered_from_the_store_are_logged(store, caplog):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    await get_chapters(chapter_get(CHAPTERS), ASIN, store=store)
    caplog.set_level(logging.WARNING, logger="libex")

    await get_chapters(outage_get, ASIN, store=store)

    assert one_served(caplog) == {"what": "chapters", "region": "us", "asin": ASIN}


async def test_a_failed_chunk_answered_from_the_store_is_logged_with_its_counts(store, caplog):
    both = asins(50) + asins(55)[50:]
    await get_books(batch_get(**{a: product(a) for a in both}), both, store=store)
    caplog.set_level(logging.INFO, logger="libex")

    await hydrate_books(chunk_failing(both[50]), both, "us", store=store)

    assert one_served(caplog) == {
        "what": "books, failed chunks", "region": "us", "stored_num": 5, "requested_num": 5,
    }
    requested = [r for r in caplog.records if r.getMessage() == "Requested books from Audible"]
    assert requested[-1].from_store_asins == 5


async def test_a_request_with_nothing_from_the_store_logs_none_and_counts_zero(store, caplog):
    caplog.set_level(logging.INFO, logger="libex")

    await hydrate_books(batch_get(**{ASIN: product(ASIN)}), [ASIN], "us", store=store)

    assert served_records(caplog) == []
    requested = [r for r in caplog.records if r.getMessage() == "Requested books from Audible"]
    assert requested[0].from_store_asins == 0


async def test_a_whole_request_outage_answered_from_the_store_is_logged(store, caplog):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    caplog.set_level(logging.WARNING, logger="libex")

    await get_books(outage_get, [ASIN, OTHER], store=store)

    assert one_served(caplog) == {
        "what": "books", "region": "us", "stored_num": 1, "requested_num": 2,
    }
    assert not [r for r in caplog.records if "Served books" in r.getMessage()]


def compound_get(catalog):
    async def get(region, path, params=None, extra_headers=None):
        if "searchsuggestions" in path:
            return {"model": {"items": []}}
        return await catalog(region, path, params, extra_headers)
    return get


async def test_a_compound_quick_search_the_catalog_could_not_answer_is_logged(store, caplog):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    caplog.set_level(logging.WARNING, logger="libex")
    keywords = f"{AUTHOR_NAME} - Title {ASIN}"

    await quick_search(compound_get(outage_get), keywords, store=store)

    fields = one_served(caplog)
    assert fields == {
        "what": "book search", "region": "us", "stored_num": 1, "catalog_failed": True,
    }
    assert keywords not in repr(fields) and AUTHOR_NAME not in repr(fields)


async def test_a_compound_quick_search_the_catalog_answered_empty_is_logged(store, caplog):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)
    caplog.set_level(logging.WARNING, logger="libex")

    async def empty_catalog(region, path, params=None, extra_headers=None):
        return {"products": []}

    found = await quick_search(
        compound_get(empty_catalog), f"{AUTHOR_NAME} - Title {ASIN}", store=store
    )

    assert [b.asin for b in found] == [ASIN]
    assert one_served(caplog) == {
        "what": "book search", "region": "us", "stored_num": 1, "catalog_failed": False,
    }


# Section: from_store on the book list

async def test_series_books_from_the_stored_members_say_so_on_the_list(store):
    members = {
        a: product(a, relationships=[{**SERIES_RELATION, "sequence": str(i + 1)}])
        for i, a in enumerate(BOOKS)
    }
    live = await get_series_books(batch_get(**members), SERIES, store=store)
    assert live.from_store == ()

    stored = await get_series_books(member_list_down, SERIES, store=store)

    assert stored.from_store == tuple(BOOKS)


async def test_author_books_answered_from_the_store_for_a_failed_chunk_say_so(store, monkeypatch):
    walked = asins(55)
    await get_books(batch_get(**{a: product(a) for a in walked}), walked, store=store)

    async def get(region, path, params=None, extra_headers=None):
        if params and "asins" in params:
            return await chunk_failing(walked[0])(region, path, params, extra_headers)
        return await fake_get(region, path, params, extra_headers)

    import libex_core.lookup.author_books as author_books

    async def walk(*args, **kwargs):
        return walked, True

    monkeypatch.setattr(author_books, "_walk_author_books", walk)
    result = await get_author_books(get, AUTHOR, store=store)

    assert set(result.from_store) == set(walked[:50])
    assert result.complete is True


# Section: requested order

async def test_books_for_a_failed_chunk_come_back_in_the_order_requested(store):
    both = asins(55)
    await get_books(batch_get(**{a: product(a) for a in both}), both, store=store)

    got = await get_books(chunk_failing(both[0]), both, store=store)

    assert [b.asin for b in got.books] == both


async def test_series_books_for_a_failed_chunk_come_back_in_series_order(store):
    members = asins(55)

    async def get(region, path, params=None, extra_headers=None):
        if path.endswith(SERIES):
            return {"product": {"asin": SERIES, "title": "The Series", "relationships": [
                {"relationship_to_product": "child", "relationship_type": "series",
                 "asin": a, "sort": str(i + 1)}
                for i, a in enumerate(members)
            ]}}
        return await chunk_failing(members[0])(region, path, params, extra_headers)

    await get_books(batch_get(**{a: product(a) for a in members}), members, store=store)

    got = await get_series_books(get, SERIES, store=store)

    assert [b.asin for b in got.books] == members
    assert set(got.from_store) == set(members[:50])


async def test_with_nothing_from_the_store_the_order_is_the_one_audible_gave(store):
    both = asins(5)

    got = await get_books(chunk_failing("never"), both, store=store)

    assert [b.asin for b in got.books] == list(reversed(both))


# Section: the asked region

async def test_a_book_stored_for_another_region_is_not_served_in_an_outage(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, region="us", store=store)

    assert (await get_book(outage_get, ASIN, region="us", store=store)).asin == ASIN
    with pytest.raises(AudibleAPIException):
        await get_book(outage_get, ASIN, region="de", store=store)
    with pytest.raises(AudibleAPIException):
        await get_books(outage_get, [ASIN], region="de", store=store)


async def test_a_failed_chunk_is_not_answered_from_another_regions_row(store):
    every = asins(55)
    tail = every[50:]
    await get_books(batch_get(**{a: product(a) for a in tail}), tail, region="us", store=store)

    mixed = await hydrate_books(chunk_failing(tail[0]), every, "de", store=store)

    assert mixed.from_store == []
    assert sorted(mixed.not_fetched) == sorted(tail)
    assert sorted(b["asin"] for b in mixed.books) == sorted(every[:50])

    same = await hydrate_books(chunk_failing(tail[0]), every, "us", store=store)
    assert sorted(same.from_store) == sorted(tail)


async def test_a_series_stored_for_another_region_is_not_served(store):
    await get_series(fake_get, SERIES, region="us", store=store)

    with pytest.raises(AudibleAPIException):
        await get_series(outage_get, SERIES, region="de", store=store)
    assert await store_module.stored_series(store, SERIES, "de") is None


async def test_an_author_is_served_only_for_the_region_it_was_stored_in(store):
    await get_author(fake_get, AUTHOR, region="de", store=store)

    assert (await get_author(outage_get, AUTHOR, region="de", store=store)).name == AUTHOR_NAME
    with pytest.raises(AudibleAPIException):
        await get_author(outage_get, AUTHOR, region="us", store=store)


async def test_chapters_follow_the_region_of_their_book(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, region="us", store=store)
    await get_chapters(chapter_get(CHAPTERS), ASIN, region="us", store=store)

    assert len((await get_chapters(outage_get, ASIN, region="us", store=store)).chapters) == 2
    with pytest.raises(AudibleAPIException):
        await get_chapters(outage_get, ASIN, region="de", store=store)


async def test_series_books_stored_for_another_region_are_not_served(store):
    members = {
        a: product(a, relationships=[{**SERIES_RELATION, "sequence": str(i + 1)}])
        for i, a in enumerate(BOOKS)
    }
    await get_series_books(batch_get(**members), SERIES, region="us", store=store)

    with pytest.raises(AudibleAPIException):
        await get_series_books(member_list_down, SERIES, region="de", store=store)


async def test_the_quick_search_stored_leg_is_limited_to_the_region(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, region="us", store=store)
    keywords = f"{AUTHOR_NAME} - Title {ASIN}"

    assert [b.asin for b in await quick_search(compound_get(outage_get), keywords, store=store)] \
        == [ASIN]
    with pytest.raises(AudibleAPIException):
        await quick_search(compound_get(outage_get), keywords, region="de", store=store)


async def test_a_book_another_marketplace_stored_is_not_served_for_this_one(store):
    await get_book(
        batch_get(**{ASIN: product(ASIN, subtitle="Kept")}), ASIN, region="us", store=store
    )

    served = await get_book(
        batch_get(**{ASIN: product(ASIN, subtitle="Aufgenommen")}),
        ASIN, region="de", store=store,
    )

    assert served.region == "de"
    assert served.subtitle == "Aufgenommen"


async def test_a_series_another_marketplace_stored_is_not_served_for_this_one(store):
    await get_series(fake_get, SERIES, region="us", store=store)

    served = await get_series(fake_get, SERIES, region="de", store=store)

    assert served.region == "de"


async def test_chapters_of_a_book_stored_only_for_another_marketplace_are_not_stored(
    store, caplog
):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, region="us", store=store)
    caplog.set_level(logging.INFO, logger="libex")

    served = await get_chapters(chapter_get(CHAPTERS), ASIN, region="de", store=store)

    assert len(served.chapters) == 2
    messages = [r.getMessage() for r in caplog.records]
    assert "Chapters not stored: the book is not in the store" in messages
    async with store.session() as session:
        assert await read_books.get_track(session, ASIN) is None


async def test_a_chapter_write_the_store_declined_reports_false(store, caplog):
    """The book was there when looked for and gone when written: the writer
    answers None, which is a refusal and not a stored listing."""
    from unittest.mock import AsyncMock, patch

    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, region="us", store=store)
    caplog.set_level(logging.INFO, logger="libex")

    with patch("libex_core.storage.write.write_track", new=AsyncMock(return_value=None)):
        stored = await store_module.persist_track(store, ASIN, CHAPTERS, "us")

    assert stored is False
    assert any(r.getMessage() == "Not written to the store" for r in caplog.records)


# Section: reads in chunks

async def test_stored_books_are_read_in_chunks_of_the_read_size(store, monkeypatch):
    wanted = asins(1201)
    held = [wanted[0], wanted[600], wanted[1200]]
    await get_books(batch_get(**{a: product(a) for a in held}), held, store=store)
    calls = []
    real = read_books.get_books

    async def counting(session, chunk, *args, **kwargs):
        calls.append(len(chunk))
        return await real(session, chunk, *args, **kwargs)

    monkeypatch.setattr(read_books, "get_books", counting)

    got = await store_module.stored_books(store, wanted)

    assert store_module.READ_CHUNK_SIZE == 500
    assert calls == [500, 500, 201]
    assert [b["asin"] for b in got] == held


# Section: chapters of a book the store could not be read for

async def test_a_failed_read_of_the_chapter_book_is_logged_and_writes_nothing(
    store, monkeypatch, caplog
):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, store=store)

    async def broken(session, asin, region):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(store_module, "_book_stored", broken)
    caplog.set_level(logging.INFO, logger="libex")

    chapters = await get_chapters(chapter_get(CHAPTERS), ASIN, store=store)

    assert len(chapters.chapters) == 2
    failed = [r for r in caplog.records if r.getMessage() == "Store read failed"]
    assert failed and failed[0].what == "chapter book" and failed[0].error_type == "RuntimeError"
    assert "secret detail" not in caplog.text
    assert not [r for r in caplog.records if r.getMessage().startswith("Chapters not stored")]
    async with store.session() as session:
        assert await read_books.get_track(session, ASIN) is None


async def test_an_absent_chapter_book_keeps_its_own_message(store, caplog):
    caplog.set_level(logging.INFO, logger="libex")

    await get_chapters(chapter_get(CHAPTERS), ASIN, store=store)

    assert [r for r in caplog.records
            if r.getMessage() == "Chapters not stored: the book is not in the store"]
    assert not [r for r in caplog.records if r.getMessage() == "Store read failed"]


# Section: one record per marketplace

DE_SUMMARY = "A much longer German summary that would win the description merge outright."


async def row_snapshot(store, table, key_column, key, region):
    async with store.session() as session:
        result = await session.execute(
            text(f"SELECT * FROM {table} WHERE {key_column} = :k AND region = :r"),
            {"k": key, "r": region},
        )
        return dict(result.mappings().one())


async def stored_regions(store, table, asin):
    async with store.session() as session:
        result = await session.execute(
            text(f"SELECT region FROM {table} WHERE asin = :a ORDER BY region"), {"a": asin}
        )
        return [r[0] for r in result.all()]


async def series_links(store, asin, region):
    async with store.session() as session:
        result = await session.execute(
            text("SELECT series_asin, series_region FROM book_series"
                 " WHERE book_asin = :a AND book_region = :r ORDER BY series_asin"),
            {"a": asin, "r": region},
        )
        return [tuple(r) for r in result.all()]


def series_of(book):
    return [(e.asin, e.name, e.region, e.position) for e in book.series]


async def test_a_book_written_for_de_after_us_stores_both_and_changes_neither(store, caplog):
    await get_book(
        batch_get(**{ASIN: product(ASIN, subtitle="Kept")}), ASIN, region="us", store=store
    )
    before = await row_snapshot(store, "books", "asin", ASIN, "us")
    caplog.set_level(logging.INFO, logger="libex")

    de_product = product(ASIN, subtitle="Untertitel", publisher_summary=DE_SUMMARY)
    live = await get_book(batch_get(**{ASIN: de_product}), ASIN, region="de", store=store)

    assert live.summary == DE_SUMMARY
    assert await stored_regions(store, "books", ASIN) == ["de", "us"]
    assert await row_snapshot(store, "books", "asin", ASIN, "us") == before
    assert (await row_snapshot(store, "books", "asin", ASIN, "de"))["subtitle"] == "Untertitel"
    assert not [r for r in caplog.records if "skipped" in r.getMessage()]


async def test_a_book_stored_in_us_and_uk_serves_each_region_its_own_row(store):
    await get_book(
        batch_get(**{ASIN: product(ASIN, subtitle="Colour")}), ASIN, region="us", store=store
    )
    await get_book(
        batch_get(**{ASIN: product(ASIN, subtitle="Colourful")}), ASIN, region="uk", store=store
    )

    us = await get_book(outage_get, ASIN, region="us", store=store)
    uk = await get_book(outage_get, ASIN, region="uk", store=store)

    assert (us.region, us.subtitle) == ("us", "Colour")
    assert (uk.region, uk.subtitle) == ("uk", "Colourful")
    assert [b["subtitle"] for b in await store_module.stored_books(store, [ASIN], "uk")] \
        == ["Colourful"]
    assert [b["subtitle"] for b in await store_module.stored_books(store, [ASIN], "us")] \
        == ["Colour"]


async def test_stored_books_without_a_region_answers_the_first_stored_row(store):
    await get_book(batch_get(**{ASIN: product(ASIN, subtitle="First")}), ASIN,
                   region="us", store=store)
    await get_book(batch_get(**{ASIN: product(ASIN, subtitle="Second")}), ASIN,
                   region="uk", store=store)

    rows = await store_module.stored_books(store, [ASIN])

    assert [(b["region"], b["subtitle"]) for b in rows] == [("us", "First")]


async def test_the_outage_fallback_never_serves_another_regions_copy(store):
    await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, region="us", store=store)

    with pytest.raises(AudibleAPIException):
        await get_book(outage_get, ASIN, region="uk", store=store)
    with pytest.raises(AudibleAPIException):
        await get_books(outage_get, [ASIN], region="uk", store=store)


async def test_chapters_are_kept_for_each_marketplaces_book(store):
    de_chapters = {"content_metadata": {"chapter_info": {
        "runtime_length_ms": 500,
        "chapters": [{"length_ms": 500, "start_offset_ms": 0, "title": "Eins"}],
    }}}
    for region in ("us", "de"):
        await get_book(batch_get(**{ASIN: product(ASIN)}), ASIN, region=region, store=store)
    await get_chapters(chapter_get(CHAPTERS), ASIN, region="us", store=store)
    await get_chapters(chapter_get(de_chapters), ASIN, region="de", store=store)

    us = await get_chapters(outage_get, ASIN, region="us", store=store)
    de = await get_chapters(outage_get, ASIN, region="de", store=store)

    assert [c.title for c in us.chapters] == ["One", "Two"]
    assert [c.title for c in de.chapters] == ["Eins"]


async def test_a_series_is_kept_per_region_and_each_book_links_its_own(store):
    await get_series(fake_get, SERIES, region="us", store=store)
    before = await row_snapshot(store, "series", "asin", SERIES, "us")
    de_book = product(ASIN, relationships=[{**SERIES_RELATION, "title": "Die Serie (DE)"}])

    served = await get_book(batch_get(**{ASIN: de_book}), ASIN, region="de", store=store)

    assert await stored_regions(store, "series", SERIES) == ["de", "us"]
    assert await row_snapshot(store, "series", "asin", SERIES, "us") == before
    assert await series_links(store, ASIN, "de") == [(SERIES, "de")]
    assert await series_links(store, ASIN, "us") == []
    assert series_of(served) == [(SERIES, "Die Serie (DE)", "de", "1")]


async def test_a_book_is_served_the_series_entries_it_cannot_store(store):
    unnamed = {**SERIES_RELATION, "asin": None, "title": "No Id", "sequence": "2"}
    book = product(ASIN, relationships=[{**SERIES_RELATION}, unnamed])
    get = batch_get(**{ASIN: book})

    served = await get_book(get, ASIN, region="de", store=store)
    unstored = await get_book(get, ASIN, region="de")

    assert series_of(served) == series_of(unstored)
    assert await series_links(store, ASIN, "de") == [(SERIES, "de")]
