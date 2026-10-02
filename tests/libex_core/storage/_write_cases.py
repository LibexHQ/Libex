"""
The writes both backends are put through, and the checks they must pass.

Shared by the SQLite tests and the SQLite-against-Postgres equivalence tests,
so a case is written once and is the same case on both. Nothing here knows
which database it is running on: it takes a session factory and writes through
`libex_core.storage.write` the way an embedder would, one unit of work per
`unit()`.

Every focused case states the row it expects, in terms of what Audible sent
and what Libex already held, so two backends agreeing on a wrong answer still
fail.
"""

import copy
import json
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from libex_core.storage.base import Base
from libex_core.storage.models import Author, Book, Series, Track
from libex_core.storage.write import (
    exclusive_write,
    upsert_author,
    upsert_genre,
    write_author_profile,
    write_books,
    write_series_profile,
    write_track,
)
from tests.goldens import cases as golden_cases
from tests.goldens import seam

TABLES = [
    "books", "authors", "series", "narrators", "genres", "tracks",
    "author_book", "book_narrator", "book_series", "book_genre",
    "author_genre", "series_author",
]
_STAMPS = {"created_at", "updated_at"}


def core_tables():
    return [Base.metadata.tables[name] for name in TABLES]


@asynccontextmanager
async def unit(factory):
    """One unit of work: its own session, serialised where the database needs
    it, committed at the end. An exception leaves the block without a commit."""
    async with factory() as session:
        async with exclusive_write(session):
            yield session
            await session.commit()


# ============================================================
# DUMPING STORED ROWS
# ============================================================

def _canon(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


async def dump(factory) -> dict:
    """Every stored row of every table, free of what two databases cannot be
    expected to agree on (timestamps, generated ids), keyed so a comparison
    does not depend on row order. Pivots name their authors by identity."""
    async with factory() as session:
        authors = {
            row.id: f"{row.asin}|{row.region}|{row.name}"
            for row in (await session.execute(select(Author.__table__))).all()
        }
        out = {}
        for table in core_tables():
            rows = []
            for row in (await session.execute(select(table))).mappings().all():
                item = {}
                for key, value in row.items():
                    if key in _STAMPS or (table.name == "authors" and key == "id"):
                        continue
                    if key == "author_id":
                        value = authors[value]
                    item[key] = _canon(value)
                rows.append(item)
            out[table.name] = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str))
    return out


async def stored_book(factory, asin: str) -> Book | None:
    async with factory() as session:
        return (await session.execute(select(Book).where(Book.asin == asin))).scalar_one_or_none()


async def stored_track(factory, asin: str) -> Track | None:
    async with factory() as session:
        return (await session.execute(select(Track).where(Track.asin == asin))).scalar_one_or_none()


async def stored_series(factory, asin: str) -> Series | None:
    async with factory() as session:
        return (await session.execute(select(Series).where(Series.asin == asin))).scalar_one_or_none()


async def stored_authors(factory, name: str) -> list[Author]:
    async with factory() as session:
        return list(
            (await session.execute(select(Author).where(Author.name == name).order_by(Author.id))).scalars()
        )


async def write(factory, books: list[dict]) -> None:
    async with unit(factory) as session:
        await write_books(session, books)


# ============================================================
# THE CORPUS
# ============================================================

def fixture_books() -> list[dict]:
    """Every golden product fixture, normalized, each under its own ASIN, plus
    the same fixtures as they come (they share two ASINs, across regions)."""
    out = []
    for name, (product, region) in golden_cases.product_cases().items():
        try:
            book = seam.normalize_product(product, region)
        except Exception:
            continue
        if isinstance(book, dict) and book.get("asin"):
            out.append(book)
    return out


def unique_books() -> list[dict]:
    books = []
    for index, original in enumerate(fixture_books()):
        book = copy.deepcopy(original)
        book["asin"] = f"B0U{index:07d}"
        books.append(book)
    return books


def thinned(book: dict) -> dict:
    """The same book as a thin response group would carry it: every guarded
    text blank or shortened, every list empty, every optional value absent,
    the extras reduced to one new key."""
    thin = {"asin": book["asin"], "title": "  ", "region": book["region"]}
    thin["description"] = (book.get("description") or "")[:1]
    thin["summary"] = ""
    thin["publisher"] = "\t"
    thin["imageUrl"] = ""
    thin["subtitle"] = ""
    thin["isbn"] = "　"
    thin["extendedProductDescription"] = ""
    thin["productState"] = ""
    thin["audibleExtras"] = {"zz_thin": 1}
    thin["genres"], thin["narrators"], thin["authors"], thin["series"] = [], [], [], []
    return thin


async def corpus_phases(factory) -> dict:
    """The corpus written, thinned, re-sent as it came, and enriched; the
    stored state after each step."""
    phases = {}
    rich = unique_books()
    for start in range(0, len(rich), 20):
        await write(factory, rich[start:start + 20])
    phases["rich"] = await dump(factory)

    await write(factory, [thinned(book) for book in rich])
    phases["thin"] = await dump(factory)

    await write(factory, fixture_books())
    phases["as_sent"] = await dump(factory)

    richer = []
    for book in rich[:5]:
        richer.append({
            **book,
            "description": (book.get("description") or "") + " and a much longer ending than before",
            "subtitle": "A Subtitle Written Later",
            "audibleExtras": {"added_later": {"k": [1, 2]}},
        })
    await write(factory, richer)
    phases["richer"] = await dump(factory)
    return phases


async def track_phases(factory) -> dict:
    phases = {}
    full = seam.normalize_chapters(golden_cases.chapter_cases()["full"][0], "B0TRACK001")
    empty = seam.normalize_chapters(golden_cases.chapter_cases()["empty"][0], "B0TRACK001")
    await write(factory, [{"asin": "B0TRACK001", "title": "t", "region": "us"}])
    async with unit(factory) as session:
        phases["full_count"] = await write_track(session, "B0TRACK001", full)
    phases["full"] = await dump(factory)
    async with unit(factory) as session:
        phases["empty_count"] = await write_track(session, "B0TRACK001", empty)
    phases["after_empty"] = await dump(factory)
    shrunk = {**full, "chapters": full["chapters"][:1]}
    async with unit(factory) as session:
        phases["shrunk_count"] = await write_track(session, "B0TRACK001", shrunk)
    phases["after_shrunk"] = await dump(factory)
    return phases


async def profile_phases(factory) -> dict:
    phases = {}
    async with unit(factory) as session:
        await write_author_profile(session, {
            "asin": "B0AUTHOR99", "name": "Profiled", "region": "us",
            "description": "A long author biography.", "image": "https://x/a.jpg",
            "genres": [{"asin": "G100", "name": "Fantasy", "type": "Genres"}, {"asin": None, "name": "x"}],
        })
        await write_series_profile(session, {
            "asin": "B0SERIES99", "name": "Profiled Series", "region": "us",
            "description": "A long series description.",
            "audibleExtras": {"a": {"x": 1, "y": 2}},
        })
    phases["profiles"] = await dump(factory)
    async with unit(factory) as session:
        await write_author_profile(session, {
            "asin": "B0AUTHOR99", "name": "Profiled", "region": "us",
            "description": "Short.", "image": "", "genres": [],
        })
        await write_series_profile(session, {
            "asin": "B0SERIES99", "name": "Profiled Series", "region": "uk",
            "description": "Short.", "audibleExtras": {"a": {"x": 1}},
        })
    phases["profiles_thin"] = await dump(factory)
    return phases


# ============================================================
# FOCUSED CASES
# ============================================================

def mk(asin, **fields):
    return {"asin": asin, "title": "Original Title", "region": "us", **fields}


async def case_description_never_shrinks(factory):
    await write(factory, [mk("B0FOCUS001", description="a long and detailed description")])
    await write(factory, [mk("B0FOCUS001", description="short")])
    assert (await stored_book(factory, "B0FOCUS001")).description == "a long and detailed description"
    await write(factory, [mk("B0FOCUS001", description="a long and detailed description, and more")])
    assert (await stored_book(factory, "B0FOCUS001")).description.endswith("and more")


async def case_null_is_filled_blank_is_not_a_value(factory):
    await write(factory, [mk("B0FOCUS002", description=None, publisher=None)])
    await write(factory, [mk("B0FOCUS002", description="  ", publisher=" \t")])
    book = await stored_book(factory, "B0FOCUS002")
    assert book.description is None and book.publisher is None
    await write(factory, [mk("B0FOCUS002", description="now known", publisher="Pub")])
    book = await stored_book(factory, "B0FOCUS002")
    assert (book.description, book.publisher) == ("now known", "Pub")


async def case_blank_title_cannot_blank_a_stored_one(factory):
    await write(factory, [mk("B0FOCUS003", title="Real Title")])
    for blank in ("", "   ", "　", None):
        await write(factory, [mk("B0FOCUS003", title=blank)])
        assert (await stored_book(factory, "B0FOCUS003")).title == "Real Title"
    await write(factory, [mk("B0FOCUS003", title="Renamed")])
    assert (await stored_book(factory, "B0FOCUS003")).title == "Renamed"


async def case_blank_guarded_text_keeps_the_stored_value(factory):
    full = {
        "subtitle": "Sub", "publisher": "Pub", "copyright": "(c)", "isbn": "123", "language": "english",
        "imageUrl": "https://x/i.jpg", "bookFormat": "unabridged", "contentType": "Product",
        "contentDeliveryType": "SinglePartBook", "episodeNumber": "4", "episodeType": "full",
        "sku": "S1", "skuGroup": "G1", "publicationName": "PN", "productState": "AVAILABLE",
    }
    await write(factory, [mk("B0FOCUS004", **full)])
    await write(factory, [mk("B0FOCUS004", **{key: "" for key in full})])
    book = await stored_book(factory, "B0FOCUS004")
    assert (book.subtitle, book.publisher, book.copyright, book.isbn, book.language) == ("Sub", "Pub", "(c)", "123", "english")
    assert (book.image, book.book_format, book.content_type) == ("https://x/i.jpg", "unabridged", "Product")
    assert (book.content_delivery_type, book.episode_number, book.episode_type) == ("SinglePartBook", "4", "full")
    assert (book.sku, book.sku_group, book.publication_name, book.product_state) == ("S1", "G1", "PN", "AVAILABLE")


async def case_pivots_only_grow(factory):
    genres = [{"asin": "G1", "name": "One", "type": "Genres"}, {"asin": "G2", "name": "Two", "type": "Tags"}]
    narrators = [{"name": "N One"}, {"name": "N Two"}]
    authors = [{"asin": "B0AU000001", "name": "Au One", "region": "us"}, {"asin": "B0AU000002", "name": "Au Two", "region": "us"}]
    await write(factory, [mk("B0FOCUS005", genres=genres, narrators=narrators, authors=authors)])
    await write(factory, [mk("B0FOCUS005", genres=genres[:1], narrators=narrators[:1], authors=authors[:1])])
    await write(factory, [mk("B0FOCUS005", genres=[], narrators=[], authors=[])])
    state = await dump(factory)
    assert sorted(r["genre_asin"] for r in state["book_genre"]) == ["G1", "G2"]
    assert sorted(r["narrator_name"] for r in state["book_narrator"]) == ["N One", "N Two"]
    assert len(state["author_book"]) == 2


async def case_extras_union_and_nested_shrink(factory):
    blob = {"a": {"x": 1, "y": 2}, "list": [1, 2, 3], "text": "kept"}
    await write(factory, [mk("B0FOCUS006", audibleExtras=blob)])
    for contained in ({"a": {"x": 1}}, {"list": [1]}, {"a": {"y": 2}, "text": "kept"}, {}):
        await write(factory, [mk("B0FOCUS006", audibleExtras=contained)])
        assert (await stored_book(factory, "B0FOCUS006")).audible_extras == blob
    await write(factory, [mk("B0FOCUS006", audibleExtras=None)])
    assert (await stored_book(factory, "B0FOCUS006")).audible_extras == blob
    await write(factory, [mk("B0FOCUS006", audibleExtras={"later": 1})])
    assert (await stored_book(factory, "B0FOCUS006")).audible_extras == {**blob, "later": 1}


async def case_extras_null_is_kept_until_something_is_written(factory):
    await write(factory, [mk("B0FOCUS007")])
    assert (await stored_book(factory, "B0FOCUS007")).audible_extras is None
    await write(factory, [mk("B0FOCUS007", audibleExtras=None, extrasWithheld=None)])
    book = await stored_book(factory, "B0FOCUS007")
    assert book.audible_extras is None and book.extras_withheld is None
    await write(factory, [mk("B0FOCUS007", audibleExtras={}, extrasWithheld={"relationships": {"episode": 4}})])
    book = await stored_book(factory, "B0FOCUS007")
    assert book.audible_extras == {} and book.extras_withheld == {"relationships": {"episode": 4}}
    await write(factory, [mk("B0FOCUS007", extrasWithheld={"audibleExtras": {"reason": "size"}})])
    assert (await stored_book(factory, "B0FOCUS007")).extras_withheld == {
        "relationships": {"episode": 4}, "audibleExtras": {"reason": "size"},
    }


async def case_silence_is_not_an_answer_for_booleans_and_numbers(factory):
    await write(factory, [mk(
        "B0FOCUS008", isListenable=False, isBuyable=False, isVvab=True, explicit=True, whisperSync=True,
        hasPdf=True, rating=4.5, numRatings=10, numReviews=3, lengthMinutes=600,
    )])
    await write(factory, [mk("B0FOCUS008")])
    book = await stored_book(factory, "B0FOCUS008")
    assert (book.is_listenable, book.is_buyable, book.is_vvab) == (False, False, True)
    assert (book.explicit, book.whisper_sync, book.has_pdf) == (True, True, True)
    assert (book.rating, book.num_ratings, book.num_reviews, book.length_minutes) == (4.5, 10, 3, 600)
    await write(factory, [mk("B0FOCUS008", isListenable=True, explicit=False)])
    book = await stored_book(factory, "B0FOCUS008")
    assert book.is_listenable is True and book.explicit is False


async def case_insert_defaults_for_silent_booleans(factory):
    await write(factory, [mk("B0FOCUS009")])
    book = await stored_book(factory, "B0FOCUS009")
    assert (book.is_listenable, book.is_buyable, book.is_vvab) == (True, True, False)
    assert (book.explicit, book.whisper_sync, book.has_pdf) == (False, False, False)


async def case_plans_null_keeps_and_empty_is_an_answer(factory):
    await write(factory, [mk("B0FOCUS010", plans=["Plus"])])
    await write(factory, [mk("B0FOCUS010", plans=None)])
    assert (await stored_book(factory, "B0FOCUS010")).plans == ["Plus"]
    await write(factory, [mk("B0FOCUS010", plans=[])])
    assert (await stored_book(factory, "B0FOCUS010")).plans == []


async def case_region_and_created_at_never_move(factory):
    await write(factory, [mk("B0FOCUS011", region="us")])
    first = await stored_book(factory, "B0FOCUS011")
    await write(factory, [mk("B0FOCUS011", region="uk", title="Second")])
    second = await stored_book(factory, "B0FOCUS011")
    assert second.region == "us"
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at


async def case_series_position_and_description(factory):
    series = {"asin": "B0SERIES01", "name": "Ser", "position": "2", "description": "a longer series description", "region": "us"}
    await write(factory, [mk("B0FOCUS012", series=[series])])
    await write(factory, [mk("B0FOCUS012", series=[{**series, "position": None, "description": "short", "region": "uk"}])])
    state = await dump(factory)
    assert state["book_series"] == [{"book_asin": "B0FOCUS012", "series_asin": "B0SERIES01", "position": "2"}]
    stored = await stored_series(factory, "B0SERIES01")
    assert stored.description == "a longer series description" and stored.region == "us"
    await write(factory, [mk("B0FOCUS012", series=[{**series, "position": "3"}])])
    assert (await dump(factory))["book_series"][0]["position"] == "3"


async def case_position_sorts_as_text_so_it_must_be_kept_as_sent(factory):
    for position in ("1", "10", "2", "1.5"):
        await write(factory, [mk(f"B0POS{position.replace('.', '')}", series=[{
            "asin": "B0SERIES02", "name": "S", "position": position, "region": "us"}])])
    assert sorted(r["position"] for r in (await dump(factory))["book_series"]) == ["1", "1.5", "10", "2"]


async def case_duplicate_asin_in_one_chunk_merges_in_order(factory):
    await write(factory, [
        mk("B0FOCUS013", description="a much longer description than the next"),
        mk("B0FOCUS013", description="next", title="Later Title"),
    ])
    book = await stored_book(factory, "B0FOCUS013")
    assert book.description == "a much longer description than the next"
    assert book.title == "Later Title"


async def case_authors_dedupe_and_upgrade(factory):
    await write(factory, [mk("B0FOCUS014", authors=[{"asin": None, "name": "Upgrade Me", "region": "us"}])])
    await write(factory, [mk("B0FOCUS015", authors=[{"asin": None, "name": "Upgrade Me", "region": "us"}])])
    rows = await stored_authors(factory, "Upgrade Me")
    assert len(rows) == 1 and rows[0].asin is None
    await write(factory, [mk("B0FOCUS014", authors=[{
        "asin": "B0UPGRADE1", "name": "Upgrade Me", "region": "us", "description": "bio", "image": "i.jpg"}])])
    rows = await stored_authors(factory, "Upgrade Me")
    assert len(rows) == 1 and (rows[0].asin, rows[0].description, rows[0].image) == ("B0UPGRADE1", "bio", "i.jpg")
    await write(factory, [mk("B0FOCUS014", authors=[{
        "asin": "B0UPGRADE1", "name": "Upgrade Me", "region": "us", "description": "b", "image": ""}])])
    rows = await stored_authors(factory, "Upgrade Me")
    assert len(rows) == 1 and (rows[0].description, rows[0].image) == ("bio", "i.jpg")
    state = await dump(factory)
    assert sorted((r["book_asin"], r["author_id"]) for r in state["author_book"]) == [
        ("B0FOCUS014", "B0UPGRADE1|us|Upgrade Me"), ("B0FOCUS015", "B0UPGRADE1|us|Upgrade Me"),
    ]


async def case_same_author_name_in_two_regions_is_two_authors(factory):
    await write(factory, [
        mk("B0FOCUS016", authors=[{"asin": None, "name": "Same Name", "region": "us"}]),
        mk("B0FOCUS017", region="uk", authors=[{"asin": None, "name": "Same Name", "region": "uk"}]),
    ])
    assert len(await stored_authors(factory, "Same Name")) == 2


async def case_chapters_keep_the_richer_payload(factory):
    three = {"runtimeLengthMs": 30, "chapters": [{"title": str(n), "startOffsetMs": n} for n in range(3)]}
    await write(factory, [mk("B0TRACK002")])
    async with unit(factory) as session:
        assert await write_track(session, "B0TRACK002", three) == 3
    async with unit(factory) as session:
        assert await write_track(session, "B0TRACK002", {"runtimeLengthMs": 0, "chapters": []}) == 3
    async with unit(factory) as session:
        assert await write_track(session, "B0TRACK002", {}) == 3
    async with unit(factory) as session:
        assert await write_track(session, "B0TRACK002", {"chapters": "not a list"}) == 3
    assert (await stored_track(factory, "B0TRACK002")).chapters == three
    one = {"runtimeLengthMs": 10, "chapters": [{"title": "only", "startOffsetMs": 0}]}
    async with unit(factory) as session:
        assert await write_track(session, "B0TRACK002", one) == 1
    assert (await stored_track(factory, "B0TRACK002")).chapters == one


async def case_a_track_with_no_chapters_is_stored_as_sent(factory):
    await write(factory, [mk("B0TRACK003")])
    async with unit(factory) as session:
        assert await write_track(session, "B0TRACK003", {"runtimeLengthMs": 5, "chapters": []}) == 0
    assert (await stored_track(factory, "B0TRACK003")).chapters == {"runtimeLengthMs": 5, "chapters": []}


async def case_genre_name_never_blanks_and_type_is_first_written(factory):
    async with unit(factory) as session:
        assert await upsert_genre(session, {"asin": "G9", "name": "Fantasy", "type": "Genres"}) == "G9"
        assert await upsert_genre(session, {"asin": "G9", "name": "Renamed", "type": "Tags"}) == "G9"
        assert await upsert_genre(session, {"asin": "G9"}) is None
    state = await dump(factory)
    assert state["genres"] == [{"asin": "G9", "name": "Renamed", "type": "Genres"}]


async def case_upsert_author_returns_the_same_id_each_time(factory):
    async with unit(factory) as session:
        first = await upsert_author(session, {"asin": "B0RET00001", "name": "Ret", "region": "us"})
        again = await upsert_author(session, {"asin": "B0RET00001", "name": "Ret", "region": "us", "description": "d"})
        none = await upsert_author(session, {"asin": None, "name": "Ret Two", "region": "us"})
        none_again = await upsert_author(session, {"asin": None, "name": "Ret Two", "region": "us"})
        assert first == again and none == none_again and first != none
        assert await upsert_author(session, {"asin": "B0X", "name": " ", "region": "us"}) is None


FOCUSED = [
    case_description_never_shrinks,
    case_null_is_filled_blank_is_not_a_value,
    case_blank_title_cannot_blank_a_stored_one,
    case_blank_guarded_text_keeps_the_stored_value,
    case_pivots_only_grow,
    case_extras_union_and_nested_shrink,
    case_extras_null_is_kept_until_something_is_written,
    case_silence_is_not_an_answer_for_booleans_and_numbers,
    case_insert_defaults_for_silent_booleans,
    case_plans_null_keeps_and_empty_is_an_answer,
    case_region_and_created_at_never_move,
    case_series_position_and_description,
    case_position_sorts_as_text_so_it_must_be_kept_as_sent,
    case_duplicate_asin_in_one_chunk_merges_in_order,
    case_authors_dedupe_and_upgrade,
    case_same_author_name_in_two_regions_is_two_authors,
    case_chapters_keep_the_richer_payload,
    case_a_track_with_no_chapters_is_stored_as_sent,
    case_genre_name_never_blanks_and_type_is_first_written,
    case_upsert_author_returns_the_same_id_each_time,
]


# ============================================================
# WHAT A THIN RESPONSE MUST NOT HAVE COST
# ============================================================

def _by_key(rows, *keys):
    return {tuple(row[k] for k in keys): row for row in rows}


def assert_nothing_shrank(before: dict, after: dict) -> None:
    """Every row and every column the stored state held before is still held
    after, apart from the columns a later response legitimately grew: extras
    only gain keys, and nothing is removed from any table."""
    assert set(after) == set(before)
    keys = {"books": ("asin",), "authors": ("asin", "region", "name"), "series": ("asin",),
            "narrators": ("name",), "genres": ("asin",), "tracks": ("asin",)}
    for table, rows in before.items():
        if table in keys:
            old, new = _by_key(rows, *keys[table]), _by_key(after[table], *keys[table])
            assert set(new) >= set(old), table
            for key, row in old.items():
                for column, value in row.items():
                    if column in ("audible_extras", "extras_withheld", "description", "summary", "extended_product_description"):
                        continue
                    if column == "fetched_description":
                        continue
                    if value is not None:
                        assert new[key][column] == value, (table, key, column)
                for column in ("description", "summary", "extended_product_description"):
                    if row.get(column) is not None:
                        assert len(new[key][column]) >= len(row[column]), (table, key, column)
                for column in ("audible_extras", "extras_withheld"):
                    if row.get(column) is not None:
                        grown, held = json.loads(new[key][column]), json.loads(row[column])
                        assert all(k in grown for k in held), (table, key, column)
        else:
            assert all(row in after[table] for row in rows), table
