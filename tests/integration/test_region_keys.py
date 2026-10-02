"""
scripts/region_keys.py against a real Postgres.

The script's whole value is what it does to a live schema -- ALTER TABLE under
a lock timeout, CREATE INDEX CONCURRENTLY and its invalid-index trap,
CHECK NOT VALID -> VALIDATE -> SET NOT NULL -- none of which a mocked session
can exercise. The container sits at the current hosted head, which has none of
the new columns, so every test starts from, and returns to, that shape: the
fixture below strips whatever the script added, because the container outlives
the test and the shared truncation fixture only clears rows.
"""

# Standard library
import asyncio
import os

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Local
import scripts.region_keys as rk
from app.db.models import Author, Book, Series, Track, author_book, book_genre, book_narrator, book_series, series_author
from app.db.models import Genre, Narrator
from scripts.region_keys import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_STOPPED,
    FINAL_COLUMNS,
    INDEXES,
    REGION_COLUMNS,
    FinalizeAbort,
    _Stop,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _strip(engine) -> None:
    async with engine.begin() as conn:
        for col in FINAL_COLUMNS:
            await conn.execute(text(f"ALTER TABLE {col.table} DROP CONSTRAINT IF EXISTS {col.check_name}"))
        for col in REGION_COLUMNS:
            await conn.execute(text(f"ALTER TABLE {col.table} DROP COLUMN IF EXISTS {col.column}"))
        for spec in INDEXES:
            await conn.execute(text(f"DROP INDEX IF EXISTS {spec.name}"))
        await conn.execute(text("ALTER TABLE series ALTER COLUMN region DROP NOT NULL"))


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    await _strip(eng)
    yield eng
    await _strip(eng)
    await eng.dispose()


@pytest_asyncio.fixture
async def seeded(db_session, engine):
    """Five books across two regions, with every kind of link and a track."""
    s = db_session
    s.add_all(
        [Book(asin=f"B00RK00{i}", title=f"book {i}", region="us" if i % 2 else "uk") for i in range(5)]
    )
    s.add_all([Series(asin="B00RKSER1", title="s1", region="us"), Series(asin="B00RKSER2", title="s2", region="uk")])
    author = Author(name="An Author", region="us")
    s.add(author)
    s.add(Narrator(name="A Narrator"))
    s.add(Genre(asin="G1", name="g", type="Genres"))
    await s.flush()
    for i in range(5):
        asin = f"B00RK00{i}"
        await s.execute(insert(author_book).values(author_id=author.id, book_asin=asin))
        await s.execute(insert(book_narrator).values(narrator_name="A Narrator", book_asin=asin))
        await s.execute(insert(book_genre).values(genre_asin="G1", book_asin=asin))
        await s.execute(insert(book_series).values(book_asin=asin, series_asin="B00RKSER1" if i < 3 else "B00RKSER2"))
        s.add(Track(asin=asin, chapters={"chapters": []}))
    await s.execute(insert(series_author).values(series_asin="B00RKSER1", author_id=author.id))
    await s.execute(insert(series_author).values(series_asin="B00RKSER2", author_id=author.id))
    await s.commit()
    return author.id


class _StopAfter(_Stop):
    """Reads as not-requested for the first n looks, then requested."""

    def __init__(self, n: int) -> None:
        self._n = n
        self._looks = 0

    @property
    def requested(self) -> bool:
        self._looks += 1
        return self._looks > self._n

    def request(self) -> None:  # pragma: no cover - never signalled in tests
        self._n = 0


async def _scalar(engine, sql: str, **params):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def _full_cycle(engine) -> None:
    stop = _Stop()
    assert await rk.expand(engine, stop) == EXIT_OK
    assert await rk.backfill(engine, stop, 2) == EXIT_OK
    assert await rk.index(engine, stop) == EXIT_OK


# --- expand ------------------------------------------------------------------

async def test_expand_adds_nullable_untyped_default_columns_and_is_idempotent(engine):
    for _ in range(2):
        assert await rk.expand(engine, _Stop()) == EXIT_OK
    async with engine.connect() as conn:
        for col in REGION_COLUMNS:
            row = (
                await conn.execute(
                    text(
                        "SELECT is_nullable, column_default, udt_name FROM information_schema.columns "
                        "WHERE table_name = :t AND column_name = :c"
                    ),
                    {"t": col.table, "c": col.column},
                )
            ).one()
            assert tuple(row) == ("YES", None, "region_enum"), col.label


async def test_expand_retries_while_a_lock_is_held_then_succeeds(engine, monkeypatch):
    monkeypatch.setattr(rk, "LOCK_TIMEOUT_MS", 150)
    monkeypatch.setattr(rk, "BACKOFF_BASE_SECONDS", 0.05)
    warnings: list[str] = []
    monkeypatch.setattr(rk.logger, "warning", lambda msg, *a, **k: warnings.append(msg))

    blocker = await engine.connect()
    await blocker.execute(text("LOCK TABLE author_book IN ACCESS EXCLUSIVE MODE"))
    task = asyncio.create_task(rk.expand(engine, _Stop()))
    await asyncio.sleep(0.6)
    await blocker.rollback()
    await blocker.close()
    assert await task == EXIT_OK
    assert any("lock not available" in w for w in warnings)
    assert await _scalar(
        engine,
        "SELECT count(*) FROM information_schema.columns WHERE table_name='author_book' AND column_name='book_region'",
    ) == 1


# --- backfill ----------------------------------------------------------------

async def test_backfill_refuses_before_expand(engine, seeded):
    assert await rk.backfill(engine, _Stop(), 2) == EXIT_FAILED


async def test_backfill_copies_the_source_region_in_batches_and_is_idempotent(engine, seeded):
    await rk.expand(engine, _Stop())
    for _ in range(2):
        assert await rk.backfill(engine, _Stop(), 2) == EXIT_OK
    for col in REGION_COLUMNS:
        assert await _scalar(engine, f"SELECT count(*) FROM {col.table} WHERE {col.column} IS NULL") == 0, col.label
    assert await _scalar(
        engine,
        "SELECT count(*) FROM author_book ab JOIN books b ON b.asin = ab.book_asin WHERE ab.book_region <> b.region",
    ) == 0
    assert await _scalar(
        engine,
        "SELECT count(*) FROM book_series bs JOIN series s ON s.asin = bs.series_asin WHERE bs.series_region <> s.region",
    ) == 0
    assert await _scalar(
        engine, "SELECT count(*) FROM tracks t JOIN books b ON b.asin = t.asin WHERE t.region <> b.region"
    ) == 0


async def test_backfill_is_resumable_after_a_stop(engine, seeded):
    await rk.expand(engine, _Stop())
    assert await rk.backfill(engine, _StopAfter(2), 1) == EXIT_STOPPED
    remaining = sum([await _scalar(engine, f"SELECT count(*) FROM {c.table} WHERE {c.column} IS NULL") for c in REGION_COLUMNS])
    assert 0 < remaining  # stopped part-way, the committed batches stayed
    total = sum([await _scalar(engine, f"SELECT count(*) FROM {c.table}") for c in REGION_COLUMNS])
    assert remaining < total
    assert await rk.backfill(engine, _Stop(), 1) == EXIT_OK
    for col in REGION_COLUMNS:
        assert await _scalar(engine, f"SELECT count(*) FROM {col.table} WHERE {col.column} IS NULL") == 0


# --- index -------------------------------------------------------------------

async def test_index_builds_valid_unique_indexes_and_is_idempotent(engine, seeded):
    await rk.expand(engine, _Stop())
    for _ in range(2):
        assert await rk.index(engine, _Stop()) == EXIT_OK
    async with engine.connect() as conn:
        for spec in INDEXES:
            assert await rk._index_state(conn, spec.name) == (True, True), spec.name


async def test_index_rebuilds_an_invalid_index(engine, seeded):
    await rk.expand(engine, _Stop())
    await rk.index(engine, _Stop())
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE pg_index SET indisvalid = false WHERE indexrelid = 'uq_books_asin_region'::regclass"))
        old = (await conn.execute(text("SELECT 'uq_books_asin_region'::regclass::oid"))).scalar_one()
    assert await rk.index(engine, _Stop()) == EXIT_OK
    async with engine.connect() as conn:
        assert await rk._index_state(conn, "uq_books_asin_region") == (True, True)
        new = (await conn.execute(text("SELECT 'uq_books_asin_region'::regclass::oid"))).scalar_one()
    assert new != old


async def test_index_refuses_before_expand(engine, seeded):
    assert await rk.index(engine, _Stop()) == EXIT_FAILED


# --- verify ------------------------------------------------------------------

async def test_verify_exit_codes(engine, seeded):
    assert await rk.verify(engine) == EXIT_FAILED  # nothing expanded
    stop = _Stop()
    await rk.expand(engine, stop)
    assert await rk.verify(engine, pre_window=True) == EXIT_FAILED  # no indexes
    await rk.backfill(engine, stop, 2)
    await rk.index(engine, stop)
    assert await rk.verify(engine, pre_window=True) == EXIT_OK
    assert await rk.verify(engine) == EXIT_FAILED  # still nullable
    assert await rk.finalize(engine, stop, 2) == EXIT_OK
    assert await rk.verify(engine) == EXIT_OK


async def test_verify_reports_nulls_as_not_ready_but_pre_window_tolerates_them(engine, seeded):
    await _full_cycle(engine)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE tracks SET region = NULL WHERE asin = 'B00RK000'"))
    assert await rk.verify(engine, pre_window=True) == EXIT_OK
    assert await rk.verify(engine) == EXIT_FAILED


# --- finalize ----------------------------------------------------------------

async def test_no_check_constraint_exists_before_finalize_and_old_inserts_still_work(engine, seeded, db_session):
    await _full_cycle(engine)
    assert await _scalar(engine, "SELECT count(*) FROM pg_constraint WHERE conname LIKE 'chk\\_%'") == 0
    # What old code does: insert link rows that know nothing of the new columns.
    await db_session.execute(insert(Book).values(asin="B00RKNEW0", title="n", region="us"))
    await db_session.execute(insert(author_book).values(author_id=seeded, book_asin="B00RKNEW0"))
    await db_session.commit()
    assert await _scalar(engine, "SELECT count(*) FROM author_book WHERE book_region IS NULL") == 1


async def test_finalize_catches_up_constrains_and_leaves_nothing_behind(engine, seeded, db_session):
    await _full_cycle(engine)
    # a row written by old code after the backfill finished
    await db_session.execute(text("UPDATE author_book SET book_region = NULL WHERE book_asin = 'B00RK001'"))
    await db_session.commit()
    before = await rk._row_counts(engine)

    assert await rk.finalize(engine, _Stop(), 2) == EXIT_OK

    assert await rk._row_counts(engine) == before
    async with engine.connect() as conn:
        for col in FINAL_COLUMNS:
            assert await rk._column_info(conn, col.table, col.column) == "NO", col.label
            assert await rk._null_count(conn, col) == 0
    assert await _scalar(engine, "SELECT count(*) FROM pg_constraint WHERE conname LIKE 'chk\\_%'") == 0
    assert await _scalar(engine, "SELECT count(*) FROM author_book WHERE book_region IS NULL") == 0


async def test_finalize_is_rerunnable_after_a_partial_run(engine, seeded):
    await _full_cycle(engine)
    # Simulate a run that died between ADD CONSTRAINT and SET NOT NULL.
    async with engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE tracks ADD CONSTRAINT chk_tracks_region_not_null CHECK (region IS NOT NULL) NOT VALID")
        )
    assert await rk.finalize(engine, _Stop(), 2) == EXIT_OK
    assert await rk.verify(engine) == EXIT_OK


async def test_finalize_aborts_before_constraining_when_a_series_has_no_region(engine, seeded, db_session):
    await _full_cycle(engine)
    await db_session.execute(text("UPDATE series SET region = NULL WHERE asin = 'B00RKSER2'"))
    await db_session.commit()
    with pytest.raises(FinalizeAbort, match="NULL region values remain"):
        await rk.finalize(engine, _Stop(), 2)
    async with engine.connect() as conn:
        for col in FINAL_COLUMNS:
            assert await rk._column_info(conn, col.table, col.column) == "YES", col.label


async def test_finalize_aborts_when_row_counts_change(engine, seeded, monkeypatch):
    await _full_cycle(engine)
    real = rk._row_counts
    calls = {"n": 0}

    async def counts(eng):
        result = await real(eng)
        calls["n"] += 1
        if calls["n"] > 1:
            result = {**result, "books": result["books"] + 1}
        return result

    monkeypatch.setattr(rk, "_row_counts", counts)
    with pytest.raises(FinalizeAbort, match="row counts changed"):
        await rk.finalize(engine, _Stop(), 2)



async def test_each_step_logs_its_elapsed_time(engine, seeded, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(rk.logger, "info", lambda msg, *a, extra=None, **k: seen.append({"msg": msg, **(extra or {})}))
    await _full_cycle(engine)
    await rk.finalize(engine, _Stop(), 2)
    steps = {e["step"] for e in seen if e["msg"] == "RegionKeys: step done"}
    assert {"expand (total)", "backfill author_book.book_region", "build uq_books_asin_region", "finalize (total)"} <= steps
    assert all("elapsed_s" in e for e in seen if e["msg"] == "RegionKeys: step done")
