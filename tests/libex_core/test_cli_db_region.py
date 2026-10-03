"""
`--region` on the db commands that read one stored record by ASIN. A book and
a series are stored once per marketplace, so the same ASIN can have a us and a
uk row: without the flag the row stored first is printed, with it that
marketplace's own, and a marketplace with none is the usual exit 3. Also that
db stats counts distinct ASINs apart from records.
"""

# Standard library
import asyncio
from datetime import timedelta

# Third party
import pytest

# Local
from libex_core.cli.environment import STORAGE_VARIABLE
from tests.libex_core._cli_lookup_support import PLANTED
from tests.libex_core._db_support import loads
from tests.libex_core.storage._support import STAMP, _book

LATER = STAMP + timedelta(days=1)


async def _add_uk_rows(path: str) -> None:
    from sqlalchemy.engine import URL

    from libex_core.storage import LocalStore
    from libex_core.storage.models import Series, Track

    store = LocalStore(URL.create("sqlite+aiosqlite", database=path))
    try:
        await store.open()
        async with store.write() as session:
            session.add(_book("B000000001", "the first quest, uk", region="uk",
                              created_at=LATER, updated_at=LATER))
            session.add(Series(asin="S000000001", title="Quest Saga UK", region="uk",
                               created_at=LATER, updated_at=LATER))
            await session.flush()
            session.add(Track(asin="B000000001", region="uk",
                              chapters={"chapters": [{"title": "Un"}], "n": 1},
                              created_at=LATER, updated_at=LATER))
    finally:
        await store.close()


@pytest.fixture
def db(run_cli, seeded_store):
    """A seeded store where B000000001 and S000000001 each have a us row,
    stored first, and a uk row stored a day later."""
    asyncio.run(_add_uk_rows(seeded_store))

    def ask(*argv):
        result = run_cli(list(argv), env={STORAGE_VARIABLE: seeded_store})
        return result.code, (loads(result.out) if result.out else None), result.err

    return ask


def ok(answer):
    code, body, err = answer
    assert code == 0, err
    return body


def test_book_without_a_region_is_the_row_stored_first(db):
    book = ok(db("db", "book", "B000000001"))
    assert (book["region"], book["title"]) == ("us", "the first quest")


def test_book_with_a_region_is_that_marketplaces_row(db):
    book = ok(db("db", "book", "B000000001", "--region", "uk"))
    assert (book["region"], book["title"]) == ("uk", "the first quest, uk")
    assert ok(db("db", "book", "B000000001", "--region", "us"))["region"] == "us"


def test_book_in_a_marketplace_that_has_none_is_exit_3_and_does_not_echo(db):
    code, body, err = db("db", "book", "B000000001", "--region", "de")
    assert (code, body) == (3, None)
    assert err == "libex-core: error: book not in the local store (code: not_in_libex)\n"


def test_chapters_follow_the_region(db):
    assert [c["title"] for c in ok(db("db", "chapters", "B000000001"))["chapters"]] == ["One"]
    uk = ok(db("db", "chapters", "B000000001", "--region", "uk"))
    assert [c["title"] for c in uk["chapters"]] == ["Un"]
    code, body, _ = db("db", "chapters", "B000000001", "--region", "de")
    assert (code, body) == (3, None)


def test_series_follows_the_region(db):
    assert ok(db("db", "series", "S000000001"))["name"] == "Quest Saga"
    assert ok(db("db", "series", "S000000001", "--region", "uk"))["name"] == "Quest Saga UK"
    code, body, _ = db("db", "series", "S000000001", "--region", "de")
    assert (code, body) == (3, None)


@pytest.mark.parametrize("command", ["book", "chapters", "series"])
def test_a_region_that_is_not_one_of_the_eleven_is_a_usage_error_that_does_not_echo_it(db, command):
    asin = "S000000001" if command == "series" else "B000000001"
    code, body, err = db("db", command, asin, "--region", PLANTED)
    assert (code, body) == (2, None)
    assert "invalid choice" in err
    assert PLANTED not in err


def test_stats_counts_records_and_distinct_asins_apart(db):
    stats = ok(db("db", "stats"))
    assert (stats["books"], stats["distinctBookAsins"]) == (16, 15)
    uk = ok(db("db", "stats", "--region", "uk"))
    assert (uk["books"], uk["distinctBookAsins"]) == (2, 2)
