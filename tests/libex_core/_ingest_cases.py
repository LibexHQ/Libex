"""
The ingest cases, written once against a store and run on SQLite and on
Postgres by the two files that import them.
"""

# Local
from libex_core.audible.books import normalize_products, settle_flags
from libex_core.lookup import _store
from libex_core.lookup.ingest import (
    ingest_author,
    ingest_books,
    ingest_chapters,
    ingest_series,
)
from libex_core.lookup.stored import (
    chapters_confirmed_at,
    stored_author,
    stored_book,
    stored_chapters,
    stored_series,
)
from libex_core.models import (
    AuthorRefResponse,
    AuthorResponse,
    BookResponse,
    ChapterResponse,
    NarratorResponse,
    SeriesResponse,
)
from libex_core.storage.models import Book
from sqlalchemy import select
from tests.libex_core._lookup_support import AUTHOR, AUTHOR_NAME, SERIES, product

ASIN = "B0INGEST01"
SERIES_RELATION = {
    "relationship_type": "series",
    "relationship_to_product": "parent",
    "asin": SERIES,
    "title": "The Series",
    "sequence": "2",
}
CHAPTERS = ChapterResponse(
    runtimeLengthMs=3000,
    runtimeLengthSec=3,
    chapters=[
        {"lengthMs": 1000, "startOffsetMs": 0, "startOffsetSec": 0, "title": "One"},
        {"lengthMs": 2000, "startOffsetMs": 1000, "startOffsetSec": 1, "title": "Two"},
    ],
)


def rich(asin=ASIN, **extra):
    return product(
        asin,
        publisher_summary="<p>A long enough description to outweigh a short one.</p>",
        publisher_name="Publisher",
        narrators=[{"name": "Nora Narrator"}],
        relationships=[SERIES_RELATION],
        category_ladders=[{"ladder": [{"id": "1", "name": "Fantasy"}]}],
        **extra,
    )


def without_clock(value):
    """The same structure without the write times, which differ between two
    stores written a moment apart."""
    if isinstance(value, dict):
        return {k: without_clock(v) for k, v in value.items() if k != "updatedAt"}
    if isinstance(value, list):
        return [without_clock(v) for v in value]
    return value


async def normalized(raw, region="us"):
    return (await normalize_products([raw], region))[0]


async def response_of(raw, region="us"):
    """The book as the hosted route serves it: normalized, then settled."""
    return BookResponse(**settle_flags(await normalized(raw, region)))


async def raw_columns(store, asin=ASIN, region="us"):
    async with store.session() as session:
        row = (await session.execute(
            select(Book).where(Book.asin == asin, Book.region == region)
        )).scalar_one()
        return {
            "explicit": row.explicit,
            "has_pdf": row.has_pdf,
            "whisper_sync": row.whisper_sync,
            "is_vvab": row.is_vvab,
            "is_listenable": row.is_listenable,
            "is_buyable": row.is_buyable,
            "plans": row.plans,
            "confirmed_at": row.confirmed_at,
            "chapters_confirmed_at": row.chapters_confirmed_at,
        }


async def case_round_trip_equals_writing_the_unsettled_dict(store, other):
    raw = rich(is_adult_product=True, is_buyable=False)
    unsettled = await normalized(raw)
    written, failed = await _store.persist_books(other, [unsettled], "us")
    assert written == {ASIN} and not failed

    assert await ingest_books(store, [await response_of(raw)], region="us") == {ASIN}

    ingested = await stored_book(store, ASIN, region="us")
    direct = await stored_book(other, ASIN, region="us")
    assert without_clock(ingested.value.model_dump()) == without_clock(direct.value.model_dump())
    assert ingested.confirmed_at is None and direct.confirmed_at is None
    assert ingested.value.explicit is True and ingested.value.isBuyable is False
    assert [s.asin for s in ingested.value.series] == [SERIES]
    assert [n.name for n in ingested.value.narrators] == ["Nora Narrator"]
    assert [a.asin for a in ingested.value.authors] == [AUTHOR]
    assert await raw_columns(store) == await raw_columns(other)


async def case_a_settled_default_never_overwrites_an_asserted_value(store):
    asserted = await normalized(rich(is_adult_product=True, is_buyable=False, is_listenable=False))
    asserted["plans"] = ["US Minerva"]
    await _store.persist_books(store, [asserted], "us")
    before = await raw_columns(store)
    assert before["explicit"] is True and before["is_buyable"] is False
    assert before["is_listenable"] is False and before["plans"] == ["US Minerva"]

    silent = await response_of(rich())
    assert silent.explicit is False and silent.isListenable is True and silent.plans == []
    await ingest_books(store, [silent], region="us")

    assert await raw_columns(store) == before


async def case_an_asserted_value_still_lands(store):
    await _store.persist_books(store, [await normalized(rich())], "us")
    book = await response_of(rich(is_adult_product=True, is_buyable=False))
    book.plans = ["US Minerva"]
    await ingest_books(store, [book], region="us")

    after = await raw_columns(store)
    assert after["explicit"] is True and after["is_buyable"] is False
    assert after["plans"] == ["US Minerva"]


async def case_ingest_never_shrinks_a_richer_row(store):
    await _store.persist_books(store, [await normalized(rich())], "us")
    before = (await stored_book(store, ASIN, region="us")).value

    thin = BookResponse(asin=ASIN, region="us", title=before.title, description="short")
    await ingest_books(store, [thin], region="us")

    after = (await stored_book(store, ASIN, region="us")).value
    a, b = without_clock(after.model_dump()), without_clock(before.model_dump())
    changed = {k: (b[k], a[k]) for k in a if a[k] != b[k]}
    # Only a field the stored row left empty may be filled; nothing else moves.
    assert changed == {"description": (None, "short")}


async def case_nothing_is_confirmed(store):
    await _store.persist_books(store, [await normalized(rich())], "us", confirm=True)
    stamped = await raw_columns(store)
    assert stamped["confirmed_at"] is not None

    await ingest_books(store, [await response_of(rich())], region="us")
    assert await ingest_chapters(store, ASIN, CHAPTERS, region="us") is True
    assert (await raw_columns(store))["confirmed_at"] == stamped["confirmed_at"]
    assert await chapters_confirmed_at(store, ASIN, region="us") is None

    await ingest_books(store, [await response_of(rich("B0INGEST02"))], region="us")
    assert (await stored_book(store, "B0INGEST02", region="us")).confirmed_at is None


async def manual_stamps(store, *asins, region="us"):
    async with store.session() as session:
        rows = (await session.execute(
            select(Book.asin, Book.manual_entry_at).where(
                Book.asin.in_(asins), Book.region == region
            )
        )).all()
    return dict(rows)


async def case_manual_entry_is_stamped_only_when_true(store):
    def book(asin, **extra):
        return BookResponse(asin=asin, region="us", title="Delisted", **extra)

    manual, false, absent = "B0MANUAL01", "B0MANUAL02", "B0MANUAL03"
    await ingest_books(
        store,
        [book(manual, manualEntry=True), book(false, manualEntry=False), book(absent)],
        region="us",
    )
    stamps = await manual_stamps(store, manual, false, absent)
    assert stamps[manual] is not None
    assert stamps[false] is None
    assert stamps[absent] is None
    assert (await stored_book(store, manual, region="us")).value.manualEntry is True
    assert (await stored_book(store, false, region="us")).value.manualEntry is False

    # a later ingest neither clears the stamp nor adds one
    first = stamps[manual]
    await ingest_books(
        store,
        [book(manual, manualEntry=False), book(false, manualEntry=True), book(absent)],
        region="us",
    )
    again = await manual_stamps(store, manual, false, absent)
    assert again[manual] == first
    assert again[false] is None
    assert again[absent] is None
    assert (await stored_book(store, manual, region="us")).confirmed_at is None


async def case_regions_are_kept_apart(store):
    book = await response_of(rich(), "uk")
    with_wrong_region = [book]
    try:
        await ingest_books(store, with_wrong_region, region="us")
    except ValueError:
        pass
    else:
        raise AssertionError("a book of another region was accepted")
    assert await stored_book(store, ASIN, region="uk") is None

    await ingest_books(store, [book], region="uk")
    assert await stored_book(store, ASIN, region="us") is None
    assert (await stored_book(store, ASIN, region="uk")).value.region == "uk"


async def case_chapters(store):
    assert await ingest_chapters(store, ASIN, CHAPTERS, region="us") is False
    await ingest_books(store, [await response_of(rich())], region="us")

    assert await ingest_chapters(store, ASIN, CHAPTERS, region="us") is True
    assert (await stored_chapters(store, ASIN, region="us")).value == CHAPTERS

    empty = ChapterResponse()
    assert await ingest_chapters(store, ASIN, empty, region="us") is False
    assert (await stored_chapters(store, ASIN, region="us")).value == CHAPTERS


async def case_series_and_author(store):
    series = SeriesResponse(
        asin=SERIES, name="The Series", region="us", description="About it",
        position="3", audibleExtras={"k": "v"},
    )
    assert await ingest_series(store, series, region="us") is True
    found = await stored_series(store, SERIES, region="us")
    assert found.value.name == "The Series" and found.value.description == "About it"
    assert found.value.audibleExtras == {"k": "v"} and found.confirmed_at is None

    shorter = SeriesResponse(asin=SERIES, name="The Series", region="us", description="A")
    await ingest_series(store, shorter, region="us")
    assert (await stored_series(store, SERIES, region="us")).value.description == "About it"

    author = AuthorResponse(
        asin=AUTHOR, name=AUTHOR_NAME, region="us", description="Bio", image="http://i",
        genres=[{"asin": "G1", "name": "Fantasy", "type": "Genres"}],
    )
    assert await ingest_author(store, author, region="us") is True
    got = await stored_author(store, AUTHOR, region="us")
    assert got.value.description == "Bio" and got.value.image == "http://i"
    assert got.value.genres[0]["name"] == "Fantasy" and got.confirmed_at is None

    bare = AuthorResponse(asin=AUTHOR, name=AUTHOR_NAME, region="us")
    await ingest_author(store, bare, region="us")
    got = await stored_author(store, AUTHOR, region="us")
    assert got.value.description == "Bio" and got.value.image == "http://i"
    assert got.confirmed_at is None


async def case_ingest_never_replaces_a_stored_chapter_listing(store):
    await ingest_books(store, [await response_of(rich())], region="us")
    confirmed = ChapterResponse(
        runtimeLengthMs=3000, runtimeLengthSec=3, isAccurate=True,
        contentReference={"acr": "x"},
        chapters=[
            {"lengthMs": 1000, "startOffsetMs": 0, "startOffsetSec": 0, "title": "One",
             "audibleExtras": {"k": "v"}},
            {"lengthMs": 2000, "startOffsetMs": 1000, "startOffsetSec": 1, "title": "Two"},
        ],
    )
    await _store.persist_track(store, ASIN, confirmed.model_dump(exclude_none=True), "us", confirm=True)

    hollow = ChapterResponse(chapters=[{}], contentReference=None, audibleExtras=None)
    assert await ingest_chapters(store, ASIN, hollow, region="us") is False
    assert (await stored_chapters(store, ASIN, region="us")).value == confirmed


async def case_ingest_stores_only_what_the_listing_carries(store):
    await ingest_books(store, [await response_of(rich())], region="us")
    listing = ChapterResponse(chapters=[{"title": "One"}], contentReference=None)
    assert await ingest_chapters(store, ASIN, listing, region="us") is True
    assert await _store.stored_track(store, ASIN, "us") == {"chapters": [{"title": "One"}]}


async def case_a_nameless_author_or_narrator_does_not_lose_the_book(store):
    book = await response_of(rich())
    book.authors.append(AuthorRefResponse(asin="B0NONAME01"))
    book.authors.append(AuthorRefResponse(asin="B0NONAME02", name="   "))
    book.narrators.append(NarratorResponse.model_construct(name=None))
    assert await ingest_books(store, [book], region="us") == {ASIN}
    got = (await stored_book(store, ASIN, region="us")).value
    assert [a.asin for a in got.authors] == [AUTHOR]
    assert [n.name for n in got.narrators] == ["Nora Narrator"]


async def case_an_author_without_a_region_takes_the_ingest_region(store):
    book = await response_of(rich(), "uk")
    for author in book.authors:
        author.region = None
    await ingest_books(store, [book], region="uk")
    got = (await stored_book(store, ASIN, region="uk")).value
    assert [a.region for a in got.authors] == ["uk"]
