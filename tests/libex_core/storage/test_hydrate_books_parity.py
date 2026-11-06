"""
hydrate_books reads the series positions of all the books it is given in one
query. It has to answer exactly what building each book with its own
region-scoped lookup answers: a series linked under one ASIN in two
marketplaces keeps each marketplace's position, a withheld-extras record stays
only on the book that has it, an author with no ASIN is kept, and the order of
the books given is the order returned.
"""

# Third party
import pytest
from sqlalchemy import event, insert, select

# Local
from libex_core.storage.models import Author, Book, Series, author_book, book_series
from libex_core.storage.read.shapes import BOOK_RELATIONS, book_to_dict, hydrate_books, series_positions
from tests.libex_core.storage._support import STAMP, _book


def _series(asin, title, region):
    return Series(asin=asin, title=title, region=region, created_at=STAMP, updated_at=STAMP, fetched_description=False)


@pytest.fixture
async def session(sqlite_store):
    async with sqlite_store.write() as s:
        s.add_all([
            _book("B0HYD00001", "Shared", region="us", extras_withheld={"k": ["x"]}),
            _book("B0HYD00001", "Shared", region="uk", is_primary=False),
            _book("B0HYD00002", "Other", region="us"),
            _book("B0HYD00003", "Alone", region="us"),
            _series("S0HYD00001", "Saga", "us"),
            _series("S0HYD00001", "Saga", "uk"),
            _series("S0HYD00002", "Side", "us"),
            Author(id=1, asin=None, name="No Asin", region="us", created_at=STAMP, updated_at=STAMP, fetched_description=False),
        ])
        await s.flush()
        await s.execute(insert(book_series).values([
            dict(book_asin="B0HYD00001", book_region="us", series_asin="S0HYD00001", series_region="us", position="1"),
            dict(book_asin="B0HYD00001", book_region="uk", series_asin="S0HYD00001", series_region="uk", position="2"),
            dict(book_asin="B0HYD00001", book_region="us", series_asin="S0HYD00002", series_region="us", position="5"),
            dict(book_asin="B0HYD00002", book_region="us", series_asin="S0HYD00001", series_region="us", position="9"),
        ]))
        await s.execute(insert(author_book).values(author_id=1, book_asin="B0HYD00002", book_region="us"))
    async with sqlite_store.session() as s:
        yield s


async def _books(session):
    result = await session.execute(select(Book).options(*BOOK_RELATIONS).order_by(Book.asin, Book.region))
    return list(result.scalars().all())


async def _per_book(session, books):
    return [book_to_dict(b, await series_positions(session, b.asin, region=b.region)) for b in books]


async def test_the_batched_answer_equals_the_per_book_answer(session):
    books = await _books(session)
    assert len(books) == 4
    assert await hydrate_books(session, books) == await _per_book(session, books)


async def test_two_regions_of_one_asin_keep_their_own_positions(session):
    by_key = {(d["asin"], d["region"]): d for d in await hydrate_books(session, await _books(session))}
    us, uk = by_key[("B0HYD00001", "us")], by_key[("B0HYD00001", "uk")]
    assert {s["asin"]: s["position"] for s in us["series"]} == {"S0HYD00001": "1", "S0HYD00002": "5"}
    assert {s["asin"]: s["position"] for s in uk["series"]} == {"S0HYD00001": "2"}
    assert by_key[("B0HYD00002", "us")]["series"][0]["position"] == "9"
    assert by_key[("B0HYD00003", "us")]["series"] == []


async def test_a_lookup_by_asin_alone_would_give_a_different_answer(session):
    """Why positions are keyed by (asin, region): read by asin alone, the two
    marketplaces' links of B0HYD00001 collapse into one and a book gets the
    other region's position or an extra series."""
    books = await _books(session)
    right = await hydrate_books(session, books)
    wrong = [book_to_dict(b, await series_positions(session, b.asin)) for b in books]
    assert wrong != right
    differing = {(r["asin"], r["region"]) for r, w in zip(right, wrong) if r != w}
    assert ("B0HYD00001", "uk") in differing or ("B0HYD00001", "us") in differing


async def test_extras_withheld_stays_on_the_book_that_has_it(session):
    by_key = {(d["asin"], d["region"]): d for d in await hydrate_books(session, await _books(session))}
    assert by_key[("B0HYD00001", "us")]["extrasWithheld"] == {"k": ["x"]}
    assert "extrasWithheld" not in by_key[("B0HYD00001", "uk")]
    assert "extrasWithheld" not in by_key[("B0HYD00003", "us")]


async def test_an_author_with_no_asin_is_kept(session):
    by_key = {(d["asin"], d["region"]): d for d in await hydrate_books(session, await _books(session))}
    [author] = by_key[("B0HYD00002", "us")]["authors"]
    assert (author["name"], author["asin"]) == ("No Asin", None)


async def test_the_order_given_is_the_order_returned(session):
    books = await _books(session)
    reversed_books = list(reversed(books))
    got = await hydrate_books(session, reversed_books)
    assert [(d["asin"], d["region"]) for d in got] == [(b.asin, b.region) for b in reversed_books]
    assert [(d["asin"], d["region"]) for d in got] != [(b.asin, b.region) for b in books]
    assert await hydrate_books(session, []) == []


async def test_the_positions_are_read_in_one_statement_not_one_per_book(session):
    books = await _books(session)
    seen = []
    engine = session.bind.sync_engine

    def record(conn, cursor, statement, *rest):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        await hydrate_books(session, books)
        batched = [s for s in seen if "FROM book_series" in s]
        seen.clear()
        await _per_book(session, books)
        looped = [s for s in seen if "FROM book_series" in s]
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(batched) == 1
    assert len(looped) == len(books) > 1
