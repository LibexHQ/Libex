"""
The write path on SQLite: the same writes, the same merges, the same refusal
to accept less than is stored. The cases are shared with the equivalence tests
(`_write_cases`), which run them on Postgres as well and compare the rows.
"""

import asyncio
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from libex_core.storage.dialect import configure_sqlite
from libex_core.storage.models import Author, Book
from libex_core.storage.store import LocalStore
from libex_core.storage.write import exclusive_write, upsert_author, write_books
from tests.libex_core.storage import _write_cases as cases


@pytest.fixture
async def engine(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'libex.db'}"
    store = LocalStore(url)
    await store.upgrade()  # the schema is the one the package's migrations make
    await store.close()
    eng = create_async_engine(url)
    configure_sqlite(eng, busy_timeout_ms=100)
    yield eng
    await eng.dispose()


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.parametrize("case", cases.FOCUSED, ids=lambda c: c.__name__)
async def test_focused_case(factory, case):
    await case(factory)


async def test_the_corpus_is_stored_and_a_thin_refresh_costs_nothing(factory):
    phases = await cases.corpus_phases(factory)
    rich, thin = phases["rich"], phases["thin"]
    assert len(rich["books"]) == len(cases.unique_books()) > 40
    assert rich["author_book"] and rich["book_genre"] and rich["book_narrator"] and rich["book_series"]
    cases.assert_nothing_shrank(rich, thin)
    assert thin["author_book"] == rich["author_book"]
    assert thin["book_genre"] == rich["book_genre"]
    assert thin["book_narrator"] == rich["book_narrator"]
    assert thin["book_series"] == rich["book_series"]
    cases.assert_nothing_shrank(thin, phases["as_sent"])  # pivots and rows only ever grow
    assert len(phases["as_sent"]["books"]) == len(rich["books"]) + 2


async def test_a_thin_response_leaves_every_guarded_column_as_it_was(factory):
    books = cases.unique_books()
    await cases.write(factory, books)
    before = {b["asin"]: await cases.stored_book(factory, b["asin"]) for b in books}
    await cases.write(factory, [cases.thinned(b) for b in books])
    for book in books:
        was, now = before[book["asin"]], await cases.stored_book(factory, book["asin"])
        for column in (
            "title", "subtitle", "publisher", "isbn", "image", "summary", "description",
            "extended_product_description", "product_state", "plans", "is_listenable",
            "is_buyable", "is_vvab", "explicit", "whisper_sync", "has_pdf", "region",
        ):
            assert getattr(now, column) == getattr(was, column), (book["asin"], column)
        if was.audible_extras is not None:
            assert {k: v for k, v in now.audible_extras.items() if k != "zz_thin"} == was.audible_extras


async def test_track_phases(factory):
    phases = await cases.track_phases(factory)
    assert phases["full_count"] == phases["empty_count"] > 1
    assert phases["shrunk_count"] == 1
    assert phases["after_empty"]["tracks"] == phases["full"]["tracks"]
    assert phases["after_shrunk"]["tracks"] != phases["full"]["tracks"]


async def test_profile_phases_never_shrink(factory):
    phases = await cases.profile_phases(factory)
    full, thin = phases["profiles"], phases["profiles_thin"]
    assert thin["authors"] == full["authors"]
    assert thin["author_genre"] == full["author_genre"] != []
    assert thin["series"] == full["series"]
    assert full["series"][0]["region"] == "us"


# ============================================================
# FAILURE IS RAISED
# ============================================================

async def test_a_failing_write_raises_and_commits_nothing(factory):
    good = cases.mk("B0RAISE001")
    bad = {"asin": "B0RAISE002", "title": "t", "region": None}
    with pytest.raises(IntegrityError):
        async with cases.unit(factory) as session:
            await write_books(session, [good, bad])
    assert await cases.stored_book(factory, "B0RAISE001") is None


async def test_a_book_without_an_asin_raises(factory):
    with pytest.raises(KeyError):
        async with cases.unit(factory) as session:
            await write_books(session, [{"title": "no asin", "region": "us"}])


# ============================================================
# A LOST RACE UNDOES ONLY ITSELF
# ============================================================

class _Racing:
    """A session whose Nth execute is followed by a competing write, as if
    another writer had landed between two of this function's statements."""

    def __init__(self, session, after, competitor):
        self._session, self._after, self._competitor, self._calls = session, after, competitor, 0

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def execute(self, *args, **kwargs):
        result = await self._session.execute(*args, **kwargs)
        self._calls += 1
        if self._calls == self._after:
            await self._session.execute(self._competitor)
        return result


async def test_losing_the_author_upgrade_race_keeps_the_rest_of_the_transaction(factory):
    await cases.write(factory, [cases.mk("B0RACE0001", authors=[{"asin": None, "name": "Racer", "region": "us"}])])
    winner = insert(Author).values(asin="B0RACER001", name="Racer", region="us", fetched_description=False)
    async with cases.unit(factory) as session:
        await write_books(session, [cases.mk("B0RACE0002")])
        racing = _Racing(session, after=2, competitor=winner)
        author_id = await upsert_author(racing, {"asin": "B0RACER001", "name": "Racer", "region": "us"})
    rows = await cases.stored_authors(factory, "Racer")
    assert {r.asin for r in rows} == {None, "B0RACER001"} and len(rows) == 2
    assert author_id == next(r.id for r in rows if r.asin == "B0RACER001")
    assert await cases.stored_book(factory, "B0RACE0002") is not None


async def test_losing_the_null_asin_insert_race_returns_the_winner(factory):
    winner = insert(Author).values(asin=None, name="Late Racer", region="us", fetched_description=False)
    async with cases.unit(factory) as session:
        await write_books(session, [cases.mk("B0RACE0003")])
        racing = _Racing(session, after=1, competitor=winner)
        author_id = await upsert_author(racing, {"asin": None, "name": "Late Racer", "region": "us"})
    rows = await cases.stored_authors(factory, "Late Racer")
    assert len(rows) == 1 and author_id == rows[0].id
    assert await cases.stored_book(factory, "B0RACE0003") is not None


# ============================================================
# SERIALISATION
# ============================================================

async def test_writers_queue_instead_of_overlapping(factory):
    active = peak = 0

    async def writer(n):
        nonlocal active, peak
        async with factory() as session:
            async with exclusive_write(session):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.005)
                await write_books(session, [cases.mk(f"B0QUEUE{n:04d}")])
                await session.commit()
                active -= 1

    await asyncio.gather(*(writer(n) for n in range(25)))
    assert peak == 1
    async with factory() as session:
        assert len((await session.execute(select(Book.asin))).all()) == 25


async def test_concurrent_writers_lose_nothing_and_never_hit_the_busy_timeout(factory):
    # The busy timeout is 100ms and thirty writers each hold the database for
    # a few milliseconds, so the queue is far longer than the timeout allows:
    # what keeps every one of them alive is the lock, not the driver's wait.
    shared_author = {"asin": "B0SHARED01", "name": "Shared Author", "region": "us"}

    async def writer(n):
        async with cases.unit(factory) as session:
            await write_books(session, [
                cases.mk(
                    f"B0CONC{n:05d}",
                    genres=[{"asin": f"GC{n}", "name": f"Genre {n}", "type": "Tags"}],
                    authors=[shared_author],
                ),
                cases.mk(
                    "B0SHARED001",
                    description="d" * (n + 1),
                    genres=[{"asin": f"GC{n}", "name": f"Genre {n}", "type": "Tags"}],
                    audibleExtras={f"k{n}": n},
                    authors=[shared_author],
                ),
            ])
            await asyncio.sleep(0.003)

    await asyncio.gather(*(writer(n) for n in range(30)))
    state = await cases.dump(factory)
    assert len(state["books"]) == 31
    shared = await cases.stored_book(factory, "B0SHARED001")
    assert shared.description == "d" * 30
    assert shared.audible_extras == {f"k{n}": n for n in range(30)}
    assert len([r for r in state["book_genre"] if r["book_asin"] == "B0SHARED001"]) == 30
    assert len(state["authors"]) == 1
    assert len(state["author_book"]) == 31


async def test_one_engines_writers_do_not_wait_on_anothers(tmp_path):
    engines = []
    for name in ("one", "two"):
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / (name + '.db')}")
        configure_sqlite(eng)
        engines.append(eng)
    active = peak = 0
    both_inside = asyncio.Event()

    async def hold(eng):
        nonlocal active, peak
        async with async_sessionmaker(eng)() as session:
            async with exclusive_write(session):
                active += 1
                peak = max(peak, active)
                if active == 2:
                    both_inside.set()
                await asyncio.wait_for(both_inside.wait(), timeout=5)
                active -= 1

    try:
        await asyncio.gather(*(hold(eng) for eng in engines))
    finally:
        for eng in engines:
            await eng.dispose()
    assert peak == 2


async def test_a_failed_unit_releases_the_lock(factory):
    with pytest.raises(RuntimeError):
        async with cases.unit(factory):
            raise RuntimeError("boom")
    await asyncio.wait_for(cases.write(factory, [cases.mk("B0AFTER0001")]), timeout=5)
    assert await cases.stored_book(factory, "B0AFTER0001") is not None


async def test_the_unit_begins_as_a_write_transaction(engine, factory):
    seen = []
    event.listen(engine.sync_engine, "before_cursor_execute", lambda c, cur, stmt, *a: seen.append(stmt))
    await cases.write(factory, [cases.mk("B0BEGIN0001")])
    assert "BEGIN IMMEDIATE" in seen
    seen.clear()
    async with factory() as session:
        await session.execute(select(Book.asin))
    assert "BEGIN IMMEDIATE" not in seen and "BEGIN" in seen


async def test_exclusive_write_does_nothing_off_sqlite():
    session = MagicMock()
    session.get_bind.return_value.dialect.name = "postgresql"
    async with exclusive_write(session):
        pass
    session.connection.assert_not_called()


def test_an_engine_outliving_its_loop_gets_a_fresh_lock(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'loops.db'}")
    configure_sqlite(eng)

    async def contended():
        async def hold():
            async with async_sessionmaker(eng)() as session:
                async with exclusive_write(session):
                    await asyncio.sleep(0.01)

        await asyncio.gather(hold(), hold())

    async def dispose():
        await eng.dispose()

    asyncio.run(contended())
    asyncio.run(contended())
    asyncio.run(dispose())


def test_the_writer_refuses_a_dialect_it_has_no_sql_for():
    from libex_core.storage.write.support import check_dialect, conflict_on_constraint, insert_for

    assert check_dialect("sqlite") == "sqlite"
    assert check_dialect("postgresql") == "postgresql"
    with pytest.raises(ValueError, match="unsupported dialect 'mysql'"):
        check_dialect("mysql")
    with pytest.raises(ValueError):
        insert_for("mysql")
    with pytest.raises(ValueError):
        conflict_on_constraint("mysql", "uq", ["a"])


def test_a_json_document_nested_past_the_limit_is_refused():
    from libex_core.storage.dialect import MAX_JSON_DEPTH, sqlite_json_merge

    def nested(depth):
        return "[" * depth + "]" * depth

    assert sqlite_json_merge(nested(MAX_JSON_DEPTH), "{}") is not None
    with pytest.raises(ValueError, match="nests deeper"):
        sqlite_json_merge(nested(MAX_JSON_DEPTH + 1), "{}")
    with pytest.raises(ValueError, match="nests deeper"):
        sqlite_json_merge(nested(100000), "{}")
