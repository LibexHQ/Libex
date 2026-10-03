"""
Behaviour of the readers on SQLite, asserted on its own so it holds without
Docker. Where the same answer must come back from Postgres, the equivalence
tests compare the two; the checks here pin the cases that matter most.
"""

# Standard library
import subprocess
import sys
from pathlib import Path

# Third party
import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Local
from libex_core.storage.models import book_series
from libex_core.storage.read import books, people, series, stats
from libex_core.storage.read._compat import NumericPosition

pytest.importorskip("aiosqlite")


def _asins(rows):
    return [r["asin"][-2:] for r in rows]


async def test_a_backslash_in_search_text_escapes_the_next_character(sqlite_session):
    # Postgres reads "AC\DC" as the pattern ACDC, so the title with a literal
    # backslash is not what it finds.
    rows = await books.search_books(sqlite_session, title="AC\\DC")
    assert _asins(rows) == ["06"]


async def test_percent_and_underscore_stay_wildcards(sqlite_session):
    assert _asins(await books.search_books(sqlite_session, title="100%")) == ["08"]
    assert _asins(await books.search_books(sqlite_session, title="pure_g")) == ["08"]


async def test_case_folding_reaches_beyond_ascii_with_a_unicode_lower(sqlite_session):
    assert _asins(await books.search_books(sqlite_session, title="émile")) == ["05"]
    assert [r["name"] for r in await people.search_narrators(sqlite_session, "ÉMILE")] == ["Émile Lecteur"]


async def test_plan_membership_matches_whole_string_elements_only(sqlite_session):
    assert sorted(_asins(await books.get_books_by_plan(sqlite_session, "Plus"))) == ["01", "02", "06", "07"]
    assert await books.get_books_by_plan(sqlite_session, "Plu") == []
    # a number or a boolean in the array is not equal to its text
    assert await books.get_books_by_plan(sqlite_session, "1") == []
    assert await books.get_books_by_plan(sqlite_session, "true") == []
    assert await books.distinct_plans(sqlite_session) == ["1", "Free", "Plus", "Premium", "true"]


async def test_narrator_language_filter_checks_the_key(sqlite_session):
    rows = await people.search_narrators(sqlite_session, "", language="English")
    assert [r["name"] for r in rows] == ["Nina Voice"]
    assert await people.search_narrators(sqlite_session, "", language="english") == []


async def test_series_books_follow_numeric_position_then_text(sqlite_session):
    rows = await series.get_series_books(sqlite_session, "S000000003")
    # 0, 1.5, 2, 007, 10, 11 numerically; then "1-3", "Book 1" and the null
    assert _asins(rows) == ["08", "12", "09", "03", "10", "11", "13", "14", "15"]


async def test_awkward_positions_are_not_numbers(sqlite_session):
    values = await _positions(sqlite_session)
    assert values == {
        "3.": None, ".5": None, "1.2.3": None, "": None, "1e3": None,
        "12abc": None, "٣": None, "1\n": None, "1-3": None, "Book 1": None, None: None,
        "007": 7.0, "0": 0.0, "1": 1.0, "1.5": 1.5, "2": 2.0, "10": 10.0, "11": 11.0,
    }


async def _positions(session):
    rows = await session.execute(
        select(book_series.c.position, NumericPosition(book_series.c.position))
    )
    return dict(rows.all())


async def test_dates_come_back_as_utc_instants(sqlite_session):
    book = await books.get_book(sqlite_session, "B000000001")
    assert book["publicationDatetime"] == "2020-05-01T07:30:00Z"
    assert book["updatedAt"] == "2026-01-01T12:00:00+00:00"


async def test_a_missing_row_is_none_or_empty_not_an_error(sqlite_session):
    assert await books.get_book(sqlite_session, "NOPE") is None
    assert await books.get_books(sqlite_session, []) == []
    assert await people.get_author(sqlite_session, "NOPE", "us") is None
    assert await series.get_series(sqlite_session, "NOPE") is None
    assert await books.get_track(sqlite_session, "NOPE") is None


async def test_counts(sqlite_session):
    assert await stats.count_stored(sqlite_session) == {
        "books": 15, "distinctBookAsins": 15, "authors": 4, "narrators": 4, "series": 4,
        "booksWithChapters": 1,
    }
    scoped = await stats.count_stored(sqlite_session, "us")
    assert scoped["seriesRegionUnknown"] == 0


async def test_a_failing_read_raises_instead_of_answering_empty():
    engine = create_async_engine("sqlite+aiosqlite://")  # no tables
    try:
        async with async_sessionmaker(engine)() as session:
            for call in (
                books.get_book(session, "X"),
                books.search_books(session),
                books.distinct_plans(session),
                people.get_author(session, "X", "us"),
                people.search_narrators(session, "x"),
                series.get_series(session, "X"),
                series.get_series_books(session, "X"),
                stats.count_stored(session),
            ):
                with pytest.raises(OperationalError):
                    await call
    finally:
        await engine.dispose()


def test_the_readers_import_nothing_from_the_hosted_app():
    code = (
        "import sys\n"
        "import libex_core.storage.read.books, libex_core.storage.read.people\n"
        "import libex_core.storage.read.series, libex_core.storage.read.stats\n"
        "import libex_core.storage.filtering, libex_core.storage.sorting\n"
        "print(sorted(m for m in sys.modules if m == 'app' or m.startswith('app.')))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, check=True,
        cwd=Path(__file__).resolve().parents[3],
    )
    assert out.stdout.strip() == "[]"
