"""
Authors, narrators and genres are served in the order Audible credits them,
on SQLite and on Postgres, and a link written before the position column
existed is still served, after the positioned ones, in a fixed order.
"""

# Third party
import pytest
from sqlalchemy import text, update
from sqlalchemy.dialects import postgresql

# Local
from libex_core.storage.models import Book, author_book, book_genre, book_narrator
from libex_core.storage.read.books import get_book
from libex_core.storage.write import books as books_module
from libex_core.storage.write import write_books

pytest.importorskip("aiosqlite")


def _book(order, asin="B0CREDIT01"):
    return {
        "asin": asin,
        "title": "Credited",
        "region": "us",
        "authors": [{"name": n, "region": "us"} for n in order],
        "narrators": [{"name": n} for n in order],
        "genres": [{"asin": f"G{n}", "name": f"Genre {n}", "type": "Genres"} for n in order],
    }


def _served(book):
    return (
        [a["name"] for a in book["authors"]],
        [n["name"] for n in book["narrators"]],
        [g["name"].removeprefix("Genre ") for g in book["genres"]],
    )


async def _write(store, books):
    async with store.write() as session:
        await write_books(session, books)


async def test_credit_order_is_served_not_alphabetical(sqlite_store):
    await _write(sqlite_store, [_book(["Z", "M", "A"])])
    async with sqlite_store.session() as session:
        book = await get_book(session, "B0CREDIT01", region="us")
    assert _served(book) == (["Z", "M", "A"],) * 3


async def test_a_rewrite_in_a_new_order_moves_position_and_keeps_every_link(sqlite_store):
    await _write(sqlite_store, [_book(["Z", "M", "A"])])
    await _write(sqlite_store, [_book(["A", "Z", "M"])])
    async with sqlite_store.session() as session:
        book = await get_book(session, "B0CREDIT01", region="us")
    assert _served(book) == (["A", "Z", "M"],) * 3


async def test_legacy_null_positions_are_served_deterministically_after_positioned_ones(sqlite_store):
    await _write(sqlite_store, [_book(["Z", "M", "A"])])
    async with sqlite_store.write() as session:
        for table in (author_book, book_narrator, book_genre):
            await session.execute(update(table).values(position=None))
    async with sqlite_store.session() as session:
        legacy = await get_book(session, "B0CREDIT01", region="us")
    names, narrators, genres = _served(legacy)
    assert narrators == ["A", "M", "Z"]
    assert genres == ["A", "M", "Z"]
    assert names == ["Z", "M", "A"]  # by author id, which is first-written order
    # The next write fills the NULLs from the credited order.
    await _write(sqlite_store, [_book(["Z", "M", "A"])])
    async with sqlite_store.session() as session:
        book = await get_book(session, "B0CREDIT01", region="us")
    assert _served(book) == (["Z", "M", "A"],) * 3


async def test_a_partly_positioned_book_serves_positioned_links_first(sqlite_store):
    await _write(sqlite_store, [_book(["Z", "M", "A"])])
    async with sqlite_store.write() as session:
        await session.execute(update(book_narrator).where(book_narrator.c.narrator_name == "Z").values(position=None))
    async with sqlite_store.session() as session:
        book = await get_book(session, "B0CREDIT01", region="us")
    assert _served(book)[1] == ["M", "A", "Z"]


def test_the_relationships_order_by_position_in_the_compiled_query():
    for rel, pivot in (("authors", "author_book"), ("narrators", "book_narrator"), ("genres", "book_genre")):
        order = [str(c.compile(dialect=postgresql.dialect())) for c in getattr(Book, rel).property.order_by]
        assert order[0] == f"{pivot}.position ASC NULLS LAST", rel


@pytest.mark.integration
async def test_postgres_serves_credit_order_when_the_planner_uses_the_index(postgres_session):
    session = postgres_session
    filler = [
        {
            "asin": f"B0F{i:07d}", "title": "Filler", "region": "us",
            "narrators": [{"name": f"Narrator {i % 40:02d}"}, {"name": f"Narrator {(i + 7) % 40:02d}"}],
            "genres": [{"asin": f"GF{i % 40:03d}", "name": "F", "type": "Genres"}],
            "authors": [{"name": f"Author {i % 60:02d}", "region": "us"}],
        }
        for i in range(1500)
    ]
    for start in range(0, len(filler), 250):
        await write_books(session, filler[start:start + 250])
        await session.commit()
    # Stored alphabetically first, then re-credited: the heap order and the
    # unique index order both differ from the credit order, so only the
    # ORDER BY can produce it.
    await write_books(session, [_book(["A", "M", "Z"])])
    await session.commit()
    await write_books(session, [_book(["Z", "M", "A"])])
    await session.commit()
    await session.execute(text("ANALYZE"))
    session.expire_all()
    book = await get_book(session, "B0CREDIT01", region="us")
    assert _served(book) == (["Z", "M", "A"],) * 3


async def _link_counts(session):
    return [
        (await session.execute(text(f"SELECT count(*) FROM {t} WHERE book_asin = 'B0CREDIT01'"))).scalar_one()
        for t in ("author_book", "book_narrator", "book_genre")
    ]


async def test_a_shorter_credit_list_serves_its_names_first_and_keeps_every_link(sqlite_store):
    await _write(sqlite_store, [_book(["Z", "M", "A"])])
    await _write(sqlite_store, [_book(["A"])])
    async with sqlite_store.session() as session:
        book = await get_book(session, "B0CREDIT01", region="us")
        counts = await _link_counts(session)
    names, narrators, genres = _served(book)
    assert [names[0], narrators[0], genres[0]] == ["A", "A", "A"]
    assert narrators[1:] == ["M", "Z"] and genres[1:] == ["M", "Z"]
    assert sorted(names) == ["A", "M", "Z"]
    assert counts == [3, 3, 3]


async def test_an_empty_credit_list_changes_no_position(sqlite_store):
    await _write(sqlite_store, [_book(["Z", "M", "A"])])
    await _write(sqlite_store, [_book([])])
    async with sqlite_store.session() as session:
        book = await get_book(session, "B0CREDIT01", region="us")
    assert _served(book) == (["Z", "M", "A"],) * 3


@pytest.mark.integration
async def test_postgres_a_shorter_credit_list_serves_its_names_first_and_keeps_every_link(postgres_session):
    session = postgres_session
    # The seeded authors carry explicit ids, so the sequence has to be moved
    # past them before a new author can be inserted.
    await session.execute(text("SELECT setval(pg_get_serial_sequence('authors', 'id'), (SELECT max(id) FROM authors))"))
    await write_books(session, [_book(["Z", "M", "A"])])
    await session.commit()
    await write_books(session, [_book(["A"])])
    await session.commit()
    session.expire_all()
    book = await get_book(session, "B0CREDIT01", region="us")
    names, narrators, genres = _served(book)
    assert [names[0], narrators[0], genres[0]] == ["A", "A", "A"]
    assert narrators[1:] == ["M", "Z"] and genres[1:] == ["M", "Z"]
    assert await _link_counts(session) == [3, 3, 3]
    await write_books(session, [_book([])])
    await session.commit()
    session.expire_all()
    assert _served(await get_book(session, "B0CREDIT01", region="us"))[1] == narrators


def _genre_book(asin, order):
    return {
        "asin": asin, "title": "T", "region": "us",
        "genres": [{"asin": f"G{n}", "name": f"Genre {n}", "type": "Genres"} for n in order],
    }


async def _genre_positions(store):
    async with store.session() as session:
        rows = await session.execute(text("SELECT book_asin, genre_asin, position FROM book_genre"))
        return {(a, g): p for a, g, p in rows}


async def test_stale_positions_are_released_in_bounded_statements_and_other_books_are_untouched(
    sqlite_store, monkeypatch
):
    asins = [f"B0BOUND{i:03d}" for i in range(6)]
    await _write(sqlite_store, [_genre_book(a, "ZMA") for a in asins + ["B0OUTSIDE1"]])
    # 2 + 3 * 1 = 5 params per rewritten book; a budget of 12 fits two books.
    monkeypatch.setattr(books_module, "_PARAM_BUDGET", 12)
    updates = []

    async with sqlite_store.write() as session:
        real = session.execute

        async def spy(stmt, *a, **k):
            if getattr(stmt, "is_update", False):
                updates.append(stmt)
            return await real(stmt, *a, **k)

        monkeypatch.setattr(session, "execute", spy)
        await write_books(session, [_genre_book(a, "A") for a in asins])

    positions = await _genre_positions(sqlite_store)
    assert len(updates) == 3
    for a in asins:
        assert positions[(a, "GA")] == 0
        assert positions[(a, "GZ")] is None and positions[(a, "GM")] is None
    assert [positions[("B0OUTSIDE1", f"G{n}")] for n in "ZMA"] == [0, 1, 2]
