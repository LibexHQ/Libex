"""
Books and series are identified by (asin, region): the same ASIN stored under
two marketplaces is two records, and the readers and writers have to treat it
that way.

Every reader that can meet two rows for one ASIN is run against a catalogue
that has exactly that, on SQLite everywhere and on Postgres where Docker is
there. The expectations are written out, not derived, so two backends agreeing
on a wrong answer still fail: when no region is asked for the answer is the
first-stored record of each ASIN, and when one is, that region's alone.
"""

# Standard library
from datetime import datetime, timedelta, timezone

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

# Local
from libex_core.storage.base import Base
from libex_core.storage.models import (
    Author,
    Book,
    Genre,
    Narrator,
    Series,
    Track,
    author_book,
    book_genre,
    book_narrator,
    book_series,
    series_author,
)
from libex_core.storage.read import books, people, series, stats
from libex_core.storage.read.shapes import series_positions
from libex_core.storage.store import LocalStore
from libex_core.storage.write import write_books, write_track
from tests.libex_core.storage._support import core_tables

pytest.importorskip("aiosqlite")

EARLY = datetime(2026, 1, 1, tzinfo=timezone.utc)
LATE = EARLY + timedelta(days=3)
NOW = datetime.now(timezone.utc).replace(microsecond=0)
DAY = timedelta(days=1)


def _book(asin, region, title, created, **kw):
    fields = dict(
        asin=asin, region=region, title=title, created_at=created, updated_at=created,
        explicit=False, whisper_sync=False, has_pdf=False, is_listenable=True,
        is_buyable=True, is_vvab=False,
    )
    fields.update(kw)
    return Book(**fields)


async def seed(session: AsyncSession) -> None:
    """X1 stored in uk first (the primary row) and us three days later; Y1 in de only; Z1 in us
    only. The uk and us records of X1 differ in every way the readers filter
    on, so a reader that picked the wrong one, or the wrong one's links, shows."""
    session.add_all([
        _book("X1", "uk", "Shared title uk", EARLY, language="english", plans=["Plus"],
              release_date=NOW - 5 * DAY, is_vvab=True, sku_group="SG", length_minutes=100,
              rating=4.0),
        _book("X1", "us", "Shared title us", LATE, language="german", plans=["Premium"],
              release_date=NOW + 5 * DAY, is_vvab=True, sku_group="SG", length_minutes=200,
              rating=2.0, is_primary=False),
        _book("Y1", "de", "Solo", EARLY, language="german", length_minutes=50),
        _book("Z1", "us", "Zed", LATE, language="english", length_minutes=60),
    ])
    session.add_all([
        Author(id=1, asin="A1", name="Uk Author", region="uk", created_at=EARLY,
               updated_at=EARLY, fetched_description=False),
        Author(id=2, asin="A1", name="Us Author", region="us", created_at=LATE,
               updated_at=LATE, fetched_description=False),
        Genre(asin="G-uk", name="Only in uk", type="Genres", created_at=EARLY, updated_at=EARLY),
        Genre(asin="G-us", name="Only in us", type="Tags", created_at=EARLY, updated_at=EARLY),
        Narrator(name="Nina", created_at=EARLY, updated_at=EARLY),
        Series(asin="S1", region="uk", title="Saga uk", created_at=EARLY, updated_at=EARLY),
        Series(asin="S1", region="us", title="Saga us", created_at=LATE, updated_at=LATE,
               is_primary=False),
        Series(asin="S2", region="de", title="Saga de", created_at=EARLY, updated_at=EARLY),
    ])
    await session.flush()
    session.add_all([
        Track(asin="X1", region="uk", chapters={"chapters": [{"t": 1}]}, created_at=EARLY,
              updated_at=EARLY),
        Track(asin="X1", region="us", chapters={"chapters": [{"t": 1}, {"t": 2}]},
              created_at=LATE, updated_at=LATE),
    ])
    await session.execute(insert(author_book), [
        {"author_id": 1, "book_asin": "X1", "book_region": "uk"},
        {"author_id": 2, "book_asin": "X1", "book_region": "us"},
        {"author_id": 2, "book_asin": "Z1", "book_region": "us"},
    ])
    await session.execute(insert(book_narrator), [
        {"narrator_name": "Nina", "book_asin": "X1", "book_region": "uk"},
        {"narrator_name": "Nina", "book_asin": "X1", "book_region": "us"},
        {"narrator_name": "Nina", "book_asin": "Y1", "book_region": "de"},
    ])
    await session.execute(insert(book_genre), [
        {"book_asin": "X1", "book_region": "uk", "genre_asin": "G-uk"},
        {"book_asin": "X1", "book_region": "us", "genre_asin": "G-us"},
    ])
    await session.execute(insert(book_series), [
        {"book_asin": "X1", "book_region": "uk", "series_asin": "S1", "series_region": "uk",
         "position": "1"},
        {"book_asin": "X1", "book_region": "us", "series_asin": "S1", "series_region": "us",
         "position": "2"},
        {"book_asin": "Z1", "book_region": "us", "series_asin": "S1", "series_region": "us",
         "position": "3"},
        {"book_asin": "Y1", "book_region": "de", "series_asin": "S2", "series_region": "de",
         "position": "9"},
    ])
    await session.execute(insert(series_author), [
        {"series_asin": "S1", "series_region": "uk", "author_id": 1},
        {"series_asin": "S1", "series_region": "us", "author_id": 2},
    ])
    await session.commit()


@pytest_asyncio.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
async def session(request):
    if request.param == "sqlite":
        store = LocalStore("sqlite+aiosqlite://")
        await store.upgrade()
        await store.open()
        async with store.write() as writer:
            await seed(writer)
        async with store.session() as reader:
            yield reader
        await store.close()
        return
    postgres_url = request.getfixturevalue("postgres_url")
    engine = create_async_engine(postgres_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=core_tables())
        await conn.run_sync(Base.metadata.create_all, tables=core_tables())
    async with AsyncSession(engine, expire_on_commit=False) as pg:
        await seed(pg)
        yield pg
    await engine.dispose()


def _keys(rows):
    return [(r["asin"], r["region"]) for r in rows]


# ============================================================
# ONE RECORD
# ============================================================

async def test_a_book_asked_for_without_a_region_is_the_first_stored(session):
    book = await books.get_book(session, "X1")
    assert (book["region"], book["title"]) == ("uk", "Shared title uk")
    assert [(a["name"], a["region"]) for a in book["authors"]] == [("Uk Author", "uk")]
    assert [g["asin"] for g in book["genres"]] == ["G-uk"]
    assert [(s["asin"], s["region"], s["position"]) for s in book["series"]] == [("S1", "uk", "1")]


async def test_a_book_asked_for_in_a_region_is_that_regions_record_with_its_own_links(session):
    book = await books.get_book(session, "X1", region="us")
    assert (book["region"], book["title"]) == ("us", "Shared title us")
    assert [a["name"] for a in book["authors"]] == ["Us Author"]
    assert [g["asin"] for g in book["genres"]] == ["G-us"]
    assert [(s["region"], s["position"]) for s in book["series"]] == [("us", "2")]
    assert await books.get_book(session, "X1", region="fr") is None
    assert await books.get_book(session, "NOPE") is None


async def test_a_series_asked_for_without_a_region_is_the_first_stored(session):
    assert (await series.get_series(session, "S1"))["name"] == "Saga uk"
    assert (await series.get_series(session, "S1", region="us"))["name"] == "Saga us"
    assert await series.get_series(session, "S1", region="de") is None


async def test_the_writer_marks_the_first_stored_row_of_an_asin_primary():
    """Rows written in one batch share a created_at, so the mark is what says
    which was first: the one inserted first, whatever its region code. A later
    write, for either region, never moves it."""
    store = LocalStore("sqlite+aiosqlite://")
    await store.upgrade()
    await store.open()
    try:
        async with store.write() as writer:
            await write_books(writer, [
                {"asin": "T1", "region": "us", "title": "us, first in the batch",
                 "series": [{"asin": "TS", "name": "Saga"}]},
                {"asin": "T1", "region": "uk", "title": "uk, lower code, second",
                 "series": [{"asin": "TS", "name": "Saga"}]},
            ])
        async with store.write() as writer:
            await write_books(writer, [
                {"asin": "T1", "region": "uk", "title": "uk again"},
                {"asin": "T1", "region": "de", "title": "de, a later arrival"},
            ])
        async with store.session() as reader:
            marks = {
                r: p for r, p in (await reader.execute(
                    select(Book.region, Book.is_primary).where(Book.asin == "T1")
                )).all()
            }
            series_marks = {
                r: p for r, p in (await reader.execute(
                    select(Series.region, Series.is_primary).where(Series.asin == "TS")
                )).all()
            }
            assert marks == {"us": True, "uk": False, "de": False}
            assert series_marks == {"us": True, "uk": False}
            assert (await books.get_book(reader, "T1"))["region"] == "us"
            assert [r["region"] for r in await books.get_books(reader, ["T1"])] == ["us"]
            assert [r["title"] for r in await books.search_books(reader, title="T")] == [
                "us, first in the batch"
            ]
            assert (await series.get_series(reader, "TS"))["region"] == "us"
    finally:
        await store.close()


async def test_rewriting_the_primary_row_after_another_region_arrived_keeps_it_primary():
    """The update path never reconsiders the mark. A rewrite of the primary
    row sees another region holding the ASIN, which is the condition that
    makes a new row non-primary, so an update that re-evaluated it would
    demote the primary row and leave the ASIN with none."""
    store = LocalStore("sqlite+aiosqlite://")
    await store.upgrade()
    await store.open()
    try:
        product = {"asin": "T2", "region": "us", "title": "us",
                   "series": [{"asin": "TS2", "name": "Saga"}]}
        async with store.write() as writer:
            await write_books(writer, [product])
        async with store.write() as writer:
            await write_books(writer, [{**product, "region": "uk", "title": "uk"}])
        async with store.write() as writer:
            await write_books(writer, [{**product, "title": "us rewritten"}])
        async with store.session() as reader:
            marks = {
                r: p for r, p in (await reader.execute(
                    select(Book.region, Book.is_primary).where(Book.asin == "T2")
                )).all()
            }
            series_marks = {
                r: p for r, p in (await reader.execute(
                    select(Series.region, Series.is_primary).where(Series.asin == "TS2")
                )).all()
            }
            assert marks == {"us": True, "uk": False}
            assert series_marks == {"us": True, "uk": False}
    finally:
        await store.close()


async def test_a_book_stored_before_regions_were_keyed_is_primary(session):
    """Every row of a store migrated from asin-only keys is the only one of its
    ASIN, and the column's default makes it primary."""
    rows = (await session.execute(select(Book.asin, Book.region, Book.is_primary))).all()
    assert {(a, r): p for a, r, p in rows} == {
        ("X1", "uk"): True, ("X1", "us"): False, ("Y1", "de"): True, ("Z1", "us"): True,
    }


async def test_an_author_asked_for_without_a_book_region_returns_every_linked_book(session):
    """Narrowing to one marketplace is something the caller asks for; the
    default is every linked record, a non-primary one included."""
    await session.execute(
        insert(author_book).values(author_id=2, book_asin="Y1", book_region="de")
    )
    await session.commit()
    default = await people.get_author_books(session, "A1", "us")
    assert sorted(_keys(default)) == [("X1", "us"), ("Y1", "de"), ("Z1", "us")]
    narrowed = await people.get_author_books(session, "A1", "us", book_region="de")
    assert _keys(narrowed) == [("Y1", "de")]


async def test_chapters_follow_the_same_rule(session):
    assert await books.get_track(session, "X1") == {"chapters": [{"t": 1}]}
    assert await books.get_track(session, "X1", region="us") == {"chapters": [{"t": 1}, {"t": 2}]}
    assert await books.get_track(session, "X1", region="de") is None
    assert await books.get_track(session, "Y1") is None


async def test_series_positions_are_read_for_the_books_own_region(session):
    assert await series_positions(session, "X1", region="uk") == {"S1": "1"}
    assert await series_positions(session, "X1", region="us") == {"S1": "2"}


# ============================================================
# MANY RECORDS
# ============================================================

async def test_many_books_by_asin_come_back_one_per_asin_unless_a_region_is_named(session):
    rows = await books.get_books(session, ["X1", "Y1", "Z1", "NOPE"])
    assert sorted(_keys(rows)) == [("X1", "uk"), ("Y1", "de"), ("Z1", "us")]
    rows = await books.get_books(session, ["X1", "Y1", "Z1"], region="us")
    assert sorted(_keys(rows)) == [("X1", "us"), ("Z1", "us")]
    assert await books.get_books(session, []) == []


async def test_a_long_list_of_asins_is_read_in_chunks(session):
    asins = [f"N{i:05d}" for i in range(12000)] + ["X1"]
    assert _keys(await books.get_books(session, asins)) == [("X1", "uk")]


async def test_a_sku_group_returns_every_regions_variant_ordered_by_region_then_asin(session):
    assert _keys(await books.get_books_by_sku(session, "SG")) == [("X1", "uk"), ("X1", "us")]


async def test_a_search_collapses_to_one_record_per_asin(session):
    rows = await books.search_books(session, title="shared", sort="title")
    assert _keys(rows) == [("X1", "uk")]
    rows = await books.search_books(session, sort="lengthMinutes", order="asc", limit=10)
    assert _keys(rows) == [("Y1", "de"), ("Z1", "us"), ("X1", "uk")]


async def test_a_search_in_a_region_finds_only_that_regions_records(session):
    rows = await books.search_books(session, region="us", sort="lengthMinutes")
    assert _keys(rows) == [("Z1", "us"), ("X1", "us")]


async def test_a_filter_is_tested_against_the_primary_record(session):
    # The uk record is first stored but is English; only the us one is German.
    # A list shows the record a lookup shows, so X1 is an English book here
    # and the German filter does not find it; asking for the us region does.
    rows = await books.search_books(session, language="german", sort="lengthMinutes")
    assert _keys(rows) == [("Y1", "de")]
    rows = await books.search_books(session, language="german", region="us")
    assert _keys(rows) == [("X1", "us")]


async def test_pages_count_asins_not_records(session):
    first = await books.search_books(session, sort="lengthMinutes", limit=2, page=1)
    second = await books.search_books(session, sort="lengthMinutes", limit=2, page=2)
    assert _keys(first) + _keys(second) == [("Y1", "de"), ("Z1", "us"), ("X1", "uk")]


async def test_a_relationship_filter_matches_the_record_the_link_belongs_to(session):
    # "Us Author" is linked to the us record of X1 and to Z1, not to the uk record.
    rows = await books.search_books(session, author_name="us author", sort="lengthMinutes")
    assert _keys(rows) == [("Z1", "us")]
    rows = await books.search_books(session, author_name="us author", region="us", sort="lengthMinutes")
    assert _keys(rows) == [("Z1", "us"), ("X1", "us")]
    rows = await books.search_books(session, genre="only in uk")
    assert _keys(rows) == [("X1", "uk")]
    assert _keys(await books.search_books(session, category="G-us")) == []
    rows = await books.search_books(session, category="G-us", region="us")
    assert _keys(rows) == [("X1", "us")]
    rows = await books.search_books(session, series_name="saga us", sort="lengthMinutes")
    assert _keys(rows) == [("Z1", "us")]


async def test_plan_lists_find_a_book_by_the_record_that_has_the_plan(session):
    assert _keys(await books.get_books_by_plan(session, "Plus")) == [("X1", "uk")]
    assert _keys(await books.get_books_by_plan(session, "Premium")) == []
    assert _keys(await books.get_books_by_plan(session, "Premium", region="us")) == [("X1", "us")]
    assert _keys(await books.get_books_by_plan(session, "Premium", region="uk")) == []


async def test_vvab_new_release_and_coming_soon_lists_are_one_record_per_asin(session):
    assert _keys(await books.get_vvab_books(session)) == [("X1", "uk")]
    assert _keys(await books.get_vvab_books(session, region="us")) == [("X1", "us")]
    # uk released five days ago, us releases in five.
    assert _keys(await books.get_new_releases(session, days=30)) == [("X1", "uk")]
    assert _keys(await books.get_coming_soon(session, days=30)) == []
    assert _keys(await books.get_coming_soon(session, days=30, region="us")) == [("X1", "us")]


async def test_a_series_lists_each_book_once_with_the_position_of_the_record_shown(session):
    rows = await series.get_series_books(session, "S1")
    assert [(r["asin"], r["region"], r["series"][0]["position"]) for r in rows] == [
        ("X1", "uk", "1"), ("Z1", "us", "3"),
    ]
    in_us = await series.get_series_books(session, "S1", region="us")
    assert [(r["asin"], r["series"][0]["position"]) for r in in_us] == [("X1", "2"), ("Z1", "3")]


async def test_series_search_returns_one_record_per_series_asin(session):
    rows = await series.search_series(session, "saga")
    assert sorted((r["asin"], r["region"]) for r in rows) == [("S1", "uk"), ("S2", "de")]
    rows = await series.search_series(session, "saga", region="us")
    assert [(r["asin"], r["region"]) for r in rows] == [("S1", "us")]


async def test_an_authors_books_are_those_of_the_authors_own_region(session):
    assert _keys(await people.get_author_books(session, "A1", "uk")) == [("X1", "uk")]
    assert sorted(_keys(await people.get_author_books(session, "A1", "us"))) == [
        ("X1", "us"), ("Z1", "us"),
    ]
    assert await people.get_author_books(session, "A1", "de") == []
    assert await people.get_author_book_asins(session, "A1", "uk") == ["X1"]
    assert sorted(await people.get_author_book_asins(session, "A1", "us")) == ["X1", "Z1"]


async def test_a_narrators_books_are_one_record_per_asin(session):
    rows = await people.get_narrator_books(session, "Nina")
    assert sorted(_keys(rows)) == [("X1", "uk"), ("Y1", "de")]
    assert _keys(await people.get_narrator_books(session, "Nina", region="us")) == [("X1", "us")]


# ============================================================
# COUNTS
# ============================================================

async def test_distinct_asins_equal_the_primary_count(session):
    """distinctBookAsins counts primary rows; that must stay equal to counting
    the distinct ASINs themselves."""
    from sqlalchemy import distinct, func

    expected = (await session.execute(select(func.count(distinct(Book.asin))))).scalar_one()
    assert (await stats.count_stored(session))["distinctBookAsins"] == expected == 3


async def test_stored_counts_separate_records_from_asins(session):
    counts = await stats.count_stored(session)
    assert counts["books"] == 4
    assert counts["distinctBookAsins"] == 3
    assert counts["series"] == 3
    assert counts["booksWithChapters"] == 2
    uk = await stats.count_stored(session, "uk")
    assert (uk["books"], uk["distinctBookAsins"], uk["booksWithChapters"], uk["series"]) == (1, 1, 1, 1)
    us = await stats.count_stored(session, "us")
    assert (us["books"], us["distinctBookAsins"], us["booksWithChapters"], us["series"]) == (2, 2, 1, 1)
    assert us["seriesRegionUnknown"] == 0


# ============================================================
# WRITING
# ============================================================

@pytest_asyncio.fixture
async def store(tmp_path):
    local = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'w.db'}")
    await local.upgrade()
    await local.open()
    yield local
    await local.close()


def _product(region, **kw):
    book = {
        "asin": "X1", "region": region, "title": f"title {region}",
        "authors": [{"asin": "A1", "name": f"author {region}", "region": region}],
        "narrators": [{"name": "Nina"}],
        "genres": [{"asin": "G1", "name": "Epic", "type": "Genres"}],
        "series": [{"asin": "S1", "name": f"saga {region}", "position": "1" if region == "uk" else "2"}],
    }
    book.update(kw)
    return book


async def _write(store, *products):
    async with store.write() as session:
        await write_books(session, list(products))
        await session.commit()


async def _rows(store, table, *columns):
    async with store.session() as session:
        result = await session.execute(select(*[table.c[c] for c in columns]))
        return sorted(tuple(r) for r in result.all())


async def test_a_second_region_is_its_own_book_and_overwrites_nothing(store):
    await _write(store, _product("uk"))
    await _write(store, _product("us"))
    assert await _rows(store, Book.__table__, "asin", "region", "title") == [
        ("X1", "uk", "title uk"), ("X1", "us", "title us"),
    ]
    assert await _rows(store, Series.__table__, "asin", "region", "title") == [
        ("S1", "uk", "saga uk"), ("S1", "us", "saga us"),
    ]


async def test_the_do_nothing_links_keep_the_second_regions_rows(store):
    await _write(store, _product("uk"))
    await _write(store, _product("us"))
    assert await _rows(store, book_narrator, "book_asin", "book_region", "narrator_name") == [
        ("X1", "uk", "Nina"), ("X1", "us", "Nina"),
    ]
    assert await _rows(store, book_genre, "book_asin", "book_region", "genre_asin") == [
        ("X1", "uk", "G1"), ("X1", "us", "G1"),
    ]
    assert len(await _rows(store, author_book, "author_id", "book_asin", "book_region")) == 2
    assert await _rows(
        store, book_series, "book_asin", "book_region", "series_asin", "series_region", "position"
    ) == [("X1", "uk", "S1", "uk", "1"), ("X1", "us", "S1", "us", "2")]
    assert [r[:2] for r in await _rows(store, series_author, "series_asin", "series_region", "author_id")] == [
        ("S1", "uk"), ("S1", "us"),
    ]


async def test_one_chunk_holding_the_same_asin_twice_writes_both_regions_with_their_links(store):
    await _write(store, _product("uk"), _product("us"))
    assert len(await _rows(store, Book.__table__, "asin", "region")) == 2
    assert len(await _rows(store, book_narrator, "book_asin", "book_region", "narrator_name")) == 2
    assert len(await _rows(store, author_book, "author_id", "book_asin", "book_region")) == 2
    assert len(await _rows(store, book_series, "book_asin", "book_region", "series_asin", "series_region")) == 2


async def test_writing_a_region_again_adds_no_rows_and_moves_no_position_of_the_other(store):
    await _write(store, _product("uk"), _product("us"))
    await _write(store, _product("uk", series=[{"asin": "S1", "name": "saga uk", "position": "7"}]))
    assert await _rows(
        store, book_series, "book_asin", "book_region", "series_asin", "series_region", "position"
    ) == [("X1", "uk", "S1", "uk", "7"), ("X1", "us", "S1", "us", "2")]
    assert len(await _rows(store, Book.__table__, "asin", "region")) == 2


async def test_a_series_that_names_no_region_takes_the_books(store):
    await _write(store, _product("fr", series=[{"asin": "S1", "name": "saga", "position": "1"}]))
    assert await _rows(store, Series.__table__, "asin", "region") == [("S1", "fr")]


async def test_a_listing_is_written_under_its_own_region_and_merges_only_there(store):
    await _write(store, _product("uk"), _product("us"))
    async with store.write() as session:
        await write_track(session, "X1", {"chapters": [{"t": 1}, {"t": 2}]}, region="uk")
        await write_track(session, "X1", {"chapters": [{"t": 1}]}, region="us")
        await session.commit()
    async with store.write() as session:
        # A thinner answer for uk is refused; us has nothing richer to lose.
        kept = await write_track(session, "X1", {"chapters": []}, region="uk")
        await session.commit()
    assert kept == 2
    async with store.session() as session:
        assert await books.get_track(session, "X1", region="uk") == {"chapters": [{"t": 1}, {"t": 2}]}
        assert await books.get_track(session, "X1", region="us") == {"chapters": [{"t": 1}]}


async def test_a_listing_needs_its_region(store):
    async with store.write() as session:
        with pytest.raises(TypeError):
            await write_track(session, "X1", {"chapters": []})


async def test_a_listing_for_a_book_that_is_not_stored_in_that_region_is_skipped(store):
    """Nothing is written and nothing raises: the check is part of the insert,
    so a region the book is not stored in costs no failed statement."""
    await _write(store, _product("uk"))
    async with store.write() as session:
        assert await write_track(session, "X1", {"chapters": [{"t": 1}]}, region="us") is None
        assert await write_track(session, "X1", {"chapters": [{"t": 1}]}, region="uk") == 1
        await session.commit()
    assert [r[1] for r in await _rows(store, Track.__table__, "asin", "region")] == ["uk"]


async def test_deleting_one_region_takes_only_its_links(store):
    await _write(store, _product("uk"), _product("us"))
    async with store.write() as session:
        await session.execute(Book.__table__.delete().where(Book.region == "uk"))
        await session.commit()
    assert [r[1] for r in await _rows(store, book_narrator, "book_asin", "book_region")] == ["us"]
    assert [r[1] for r in await _rows(store, book_genre, "book_asin", "book_region")] == ["us"]



# ============================================================
# A SERIES ALWAYS HAS A REGION
# ============================================================

async def test_a_series_write_with_no_region_anywhere_is_refused_not_guessed(store):
    from libex_core.storage.write import upsert_series, write_series_profile

    async with store.write() as session:
        assert await upsert_series(session, {"asin": "S1", "name": "No region"}) is None
        assert await write_series_profile(session, {"asin": "S1", "name": "No region"}) is None
        await session.commit()
    assert await _rows(store, Series.__table__, "asin", "region") == []


async def test_a_series_entry_too_thin_to_store_gets_no_links_and_costs_the_chunk_nothing(store):
    """An entry with an asin and no name makes no series row, so it must make no
    link to one either: a link to a missing row breaks the key for the chunk."""
    await _write(store, _product("us", series=[
        {"asin": "THIN", "position": "1"},
        {"asin": "S1", "name": "saga", "position": "2"},
    ]))
    assert await _rows(store, Series.__table__, "asin", "region") == [("S1", "us")]
    assert await _rows(store, book_series, "book_asin", "book_region", "series_asin", "series_region") == [
        ("X1", "us", "S1", "us"),
    ]
    assert await _rows(store, series_author, "series_asin", "series_region") == [("S1", "us")]
    assert await _rows(store, Book.__table__, "asin", "region") == [("X1", "us")]


async def test_a_thin_entry_naming_an_already_stored_series_keeps_its_author_link(store):
    """The row exists from an earlier write, so the link is good even though
    this call wrote no series row."""
    await _write(store, _product("us"))
    async with store.write() as session:
        await session.execute(series_author.delete())
    await _write(store, _product("us", series=[{"asin": "S1", "position": "3"}]))
    assert await _rows(store, series_author, "series_asin", "series_region") == [("S1", "us")]


async def test_a_series_inside_a_book_with_no_region_of_its_own_takes_the_books(store):
    await _write(store, _product("de", series=[{"asin": "S7", "name": "Inside", "position": "1"}]))
    assert await _rows(store, Series.__table__, "asin", "region") == [("S7", "de")]
    assert await _rows(store, book_series, "book_asin", "book_region", "series_asin", "series_region") == [
        ("X1", "de", "S7", "de"),
    ]
