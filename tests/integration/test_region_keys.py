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
import argparse
import asyncio

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Local
import scripts.region_keys as rk
from scripts.region_keys import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_STOPPED,
    FINAL_COLUMNS,
    ONLINE_INDEXES,
    REGION_COLUMNS,
    WINDOW_INDEXES,
    FinalizeAbort,
    _Stop,
)
from tests.integration._scratch import build_template, clone, drop_database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.timeout(300)]


PRE = "b8d2e5a71c46"

STAMP = "now()"
BOOK_COLUMNS = (
    "explicit, whisper_sync, has_pdf, is_listenable, is_buyable, is_vvab, created_at, updated_at"
)
BOOK_VALUES = f"false, false, false, true, true, false, {STAMP}, {STAMP}"


# The script runs against the schema as it is before the revision that adopts
# its work. The shared container sits at the current head, which is past that,
# so every test gets its own database cloned from one built at that revision.
# The template is built inside whichever test asks first, hence the longer
# budget than the suite's default.
@pytest.fixture(scope="module")
def template():
    name = build_template(PRE)
    yield name
    drop_database(name)


@pytest_asyncio.fixture
async def engine(template):
    name, _sync_url, async_url = clone(template)
    eng = create_async_engine(async_url, poolclass=NullPool)
    yield eng
    await eng.dispose()
    drop_database(name)


@pytest_asyncio.fixture
async def db_session(engine):
    """A session on the same scratch database, shadowing the shared one."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


async def _add_old_book(db, asin: str, region: str = "us") -> None:
    await db.execute(text(
        f"INSERT INTO books (asin, title, region, {BOOK_COLUMNS}) "
        f"VALUES ('{asin}', 'n', '{region}', {BOOK_VALUES})"
    ))


@pytest_asyncio.fixture
async def seeded(db_session, engine):
    """Five books across two regions, with every kind of link and a track, in
    the shape the schema had before region was part of any key."""
    s = db_session
    for i in range(5):
        await _add_old_book(s, f"B00RK00{i}", "us" if i % 2 else "uk")
    await s.execute(text(
        "INSERT INTO series (asin, title, region, fetched_description, created_at, updated_at) "
        f"VALUES ('B00RKSER1', 's1', 'us', false, {STAMP}, {STAMP}), "
        f"('B00RKSER2', 's2', 'uk', false, {STAMP}, {STAMP})"
    ))
    await s.execute(text(
        "INSERT INTO authors (id, name, region, fetched_description, created_at, updated_at) "
        f"VALUES (1, 'An Author', 'us', false, {STAMP}, {STAMP})"
    ))
    await s.execute(text(
        f"INSERT INTO narrators (name, created_at, updated_at) VALUES ('A Narrator', {STAMP}, {STAMP})"
    ))
    await s.execute(text(
        f"INSERT INTO genres (asin, name, type, created_at, updated_at) VALUES ('G1', 'g', 'Genres', {STAMP}, {STAMP})"
    ))
    for i in range(5):
        asin = f"B00RK00{i}"
        await s.execute(text(f"INSERT INTO author_book (author_id, book_asin) VALUES (1, '{asin}')"))
        await s.execute(text(f"INSERT INTO book_narrator (narrator_name, book_asin) VALUES ('A Narrator', '{asin}')"))
        await s.execute(text(f"INSERT INTO book_genre (genre_asin, book_asin) VALUES ('G1', '{asin}')"))
        await s.execute(text(
            f"INSERT INTO book_series (book_asin, series_asin) VALUES ('{asin}', "
            f"'{'B00RKSER1' if i < 3 else 'B00RKSER2'}')"
        ))
        await s.execute(text(
            f"INSERT INTO tracks (asin, chapters, created_at, updated_at) VALUES ('{asin}', '[]', {STAMP}, {STAMP})"
        ))
    await s.execute(text("INSERT INTO series_author (series_asin, author_id) VALUES ('B00RKSER1', 1), ('B00RKSER2', 1)"))
    await s.commit()
    return 1


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
        # every row carries its parent's region, not merely some region
        mismatched = await _scalar(
            engine,
            f"SELECT count(*) FROM {col.table} t JOIN {col.source} p ON p.asin = t.{col.key} "
            f"WHERE t.{col.column} IS DISTINCT FROM p.region",
        )
        assert mismatched == 0, col.label
        joined = await _scalar(
            engine,
            f"SELECT count(*) FROM {col.table} t JOIN {col.source} p ON p.asin = t.{col.key}",
        )
        assert joined == await _scalar(engine, f"SELECT count(*) FROM {col.table}"), col.label
    # the seed spans two regions, so a copy of the wrong parent would show
    assert await _scalar(engine, "SELECT count(DISTINCT book_region) FROM author_book") == 2
    assert await _scalar(engine, "SELECT count(DISTINCT series_region) FROM series_author") == 2


async def test_backfill_analyzes_every_touched_table_and_logs_it(engine, seeded, monkeypatch):
    await rk.expand(engine, _Stop())
    seen: list[dict] = []
    monkeypatch.setattr(rk.logger, "info", lambda msg, *a, extra=None, **k: seen.append({"msg": msg, **(extra or {})}))
    assert await rk.backfill(engine, _Stop(), 2) == EXIT_OK
    steps = {e["step"] for e in seen if e["msg"] == "RegionKeys: step done" and e["step"].startswith("analyze ")}
    assert steps == {f"analyze {t}" for t in {c.table for c in REGION_COLUMNS}}


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

async def test_index_builds_only_the_online_indexes_and_is_idempotent(engine, seeded):
    await rk.expand(engine, _Stop())
    for _ in range(2):
        assert await rk.index(engine, _Stop()) == EXIT_OK
    async with engine.connect() as conn:
        for spec in ONLINE_INDEXES:
            assert await rk._index_state(conn, spec.name) == (True, spec.unique), spec.name
        for spec in WINDOW_INDEXES:
            assert await rk._index_state(conn, spec.name) is None, spec.name
    assert {s.name for s in WINDOW_INDEXES} == {"uq_books_asin_region", "uq_series_asin_region"}


async def test_index_rebuilds_an_invalid_index(engine, seeded):
    await rk.expand(engine, _Stop())
    await rk.index(engine, _Stop())
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE pg_index SET indisvalid = false WHERE indexrelid = 'uq_tracks_asin_region'::regclass"))
        old = (await conn.execute(text("SELECT 'uq_tracks_asin_region'::regclass::oid"))).scalar_one()
    assert await rk.index(engine, _Stop()) == EXIT_OK
    async with engine.connect() as conn:
        assert await rk._index_state(conn, "uq_tracks_asin_region") == (True, True)
        new = (await conn.execute(text("SELECT 'uq_tracks_asin_region'::regclass::oid"))).scalar_one()
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
    assert await rk.verify(engine, pre_window=True) == EXIT_OK  # window indexes not required yet
    assert await rk.verify(engine) == EXIT_FAILED  # still nullable, window indexes missing
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
    await _add_old_book(db_session, "B00RKNEW0")
    await db_session.execute(text(f"INSERT INTO author_book (author_id, book_asin) VALUES ({seeded}, 'B00RKNEW0')"))
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
    async with engine.connect() as conn:
        for spec in WINDOW_INDEXES:
            assert await rk._index_state(conn, spec.name) == (True, True), spec.name


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


_ORPHANS = {
    "author_book": "INSERT INTO author_book (author_id, book_asin) VALUES ({author}, 'B00ORPHAN')",
    "book_narrator": "INSERT INTO book_narrator (narrator_name, book_asin) VALUES ('A Narrator', 'B00ORPHAN')",
    "book_genre": "INSERT INTO book_genre (genre_asin, book_asin) VALUES ('G1', 'B00ORPHAN')",
    "book_series.book_region": "INSERT INTO book_series (book_asin, series_asin) VALUES ('B00ORPHAN', 'B00RKSER1')",
    "book_series.series_region": "INSERT INTO book_series (book_asin, series_asin) VALUES ('B00RK000', 'B00ORPHSER')",
    "series_author": "INSERT INTO series_author (series_asin, author_id) VALUES ('B00ORPHSER', {author})",
}


@pytest.mark.parametrize("which", sorted(_ORPHANS))
async def test_finalize_aborts_when_a_link_row_has_no_parent(engine, seeded, which):
    await _full_cycle(engine)
    async with engine.begin() as conn:
        # Foreign keys would refuse an orphan; skipping them is how one exists.
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        await conn.execute(text(_ORPHANS[which].format(author=seeded)))
    try:
        with pytest.raises(FinalizeAbort, match="NULL region values remain"):
            await rk.finalize(engine, _Stop(), 2)
        async with engine.connect() as conn:
            for col in FINAL_COLUMNS:
                assert await rk._column_info(conn, col.table, col.column) == "YES", col.label
            for spec in WINDOW_INDEXES:
                assert await rk._index_state(conn, spec.name) is None, spec.name
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL session_replication_role = replica"))
            await conn.execute(text("DELETE FROM book_series WHERE book_asin = 'B00ORPHAN' OR series_asin = 'B00ORPHSER'"))
            for table in ("author_book", "book_narrator", "book_genre"):
                await conn.execute(text(f"DELETE FROM {table} WHERE book_asin = 'B00ORPHAN'"))
            await conn.execute(text("DELETE FROM series_author WHERE series_asin = 'B00ORPHSER'"))


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


async def test_finalize_builds_the_window_indexes_before_any_column_is_made_not_null(engine, seeded, monkeypatch):
    """A failed index build must leave the columns nullable, which 2.1.x can run against."""
    await _full_cycle(engine)

    async def failing_build(eng, spec):
        return False

    monkeypatch.setattr(rk, "_build_index", failing_build)
    with pytest.raises(FinalizeAbort, match="could not be built"):
        await rk.finalize(engine, _Stop(), 2)
    async with engine.connect() as conn:
        for col in FINAL_COLUMNS:
            assert await rk._column_info(conn, col.table, col.column) == "YES", col.label


# --- unfinalize --------------------------------------------------------------

async def test_unfinalize_restores_what_a_2_1_x_writer_needs_and_finalize_can_rerun(engine, seeded, db_session):
    await _full_cycle(engine)
    assert await rk.finalize(engine, _Stop(), 2) == EXIT_OK
    # a partial finalize's leftover CHECK must go too
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE tracks ALTER COLUMN region DROP NOT NULL"))
        await conn.execute(
            text("ALTER TABLE tracks ADD CONSTRAINT chk_tracks_region_not_null CHECK (region IS NOT NULL) NOT VALID")
        )
    assert await rk.unfinalize(engine, _Stop()) == EXIT_OK
    assert await rk.unfinalize(engine, _Stop()) == EXIT_OK  # idempotent

    async with engine.connect() as conn:
        for col in FINAL_COLUMNS:
            assert await rk._column_info(conn, col.table, col.column) == "YES", col.label
            assert not await rk._constraint_exists(conn, col.table, col.check_name), col.label
        for spec in WINDOW_INDEXES:
            assert await rk._index_state(conn, spec.name) is None, spec.name
        for spec in ONLINE_INDEXES:
            assert await rk._index_state(conn, spec.name) == (True, spec.unique), spec.name
    # the old writer's shapes work again: NULL link rows, a region-less series, a new track
    await _add_old_book(db_session, "B00RKNEW0")
    await db_session.execute(text(f"INSERT INTO author_book (author_id, book_asin) VALUES ({seeded}, 'B00RKNEW0')"))
    await db_session.execute(text(
        "INSERT INTO series (asin, title, fetched_description, created_at, updated_at) "
        f"VALUES ('B00RKSER9', 's9', false, {STAMP}, {STAMP})"
    ))
    await db_session.execute(text(
        f"INSERT INTO tracks (asin, chapters, created_at, updated_at) VALUES ('B00RKNEW0', '[]', {STAMP}, {STAMP})"
    ))
    await db_session.commit()


@pytest.mark.timeout(180)  # two full finalizes, each building indexes and 8 constraints
async def test_unfinalize_then_finalize_round_trips(engine, seeded):
    await _full_cycle(engine)
    assert await rk.finalize(engine, _Stop(), 2) == EXIT_OK
    assert await rk.unfinalize(engine, _Stop()) == EXIT_OK
    assert await rk.finalize(engine, _Stop(), 2) == EXIT_OK
    assert await rk.verify(engine) == EXIT_OK


# --- the running application during the online phase -------------------------

_OLD_UPSERT = (
    "INSERT INTO books (asin, title, region, explicit, whisper_sync, has_pdf, is_listenable, "
    "is_buyable, is_vvab, created_at, updated_at) "
    "SELECT a, 'old writer', 'us', false, false, false, true, true, false, now(), now() "
    "FROM unnest(CAST(:asins AS text[])) AS a "
    "ON CONFLICT (asin) DO UPDATE SET title = EXCLUDED.title"
)
_RACERS = 24
_ROUNDS = 60
_PER_ROUND = 40


async def _race_old_upserts(engine) -> list[BaseException]:
    """Many connections upserting the same fresh asins at once, as 2.1.x does."""
    start = asyncio.Event()

    async def racer() -> list[BaseException]:
        errors: list[BaseException] = []
        async with engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await start.wait()
            for r in range(_ROUNDS):
                asins = [f"B0R{r:03d}{i:04d}" for i in range(_PER_ROUND)]
                try:
                    await conn.execute(text(_OLD_UPSERT), {"asins": asins})
                except Exception as exc:
                    errors.append(exc)
        return errors

    tasks = [asyncio.create_task(racer()) for _ in range(_RACERS)]
    await asyncio.sleep(0.5)  # every racer connected and parked
    start.set()
    found = [e for errs in await asyncio.gather(*tasks) for e in errs]
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM books WHERE asin LIKE 'B0R%'"))
    return found


@pytest.mark.timeout(120)
async def test_online_index_mode_leaves_the_old_upsert_error_free(engine, seeded):
    await _full_cycle(engine)  # expand, backfill, index: everything before the window
    assert await _race_old_upserts(engine) == []


@pytest.mark.timeout(400)
async def test_the_window_indexes_are_what_break_the_old_upsert(engine, seeded):
    """Control: shows the race above can bite, so its clean result means something.

    The failure needs two first inserts inside a window of microseconds, so a
    single pass can miss; it retries and skips, rather than passing vacuously,
    if the race never bites on this machine.
    """
    await _full_cycle(engine)
    for spec in WINDOW_INDEXES:
        await rk._build_index(engine, spec)
    for _ in range(6):
        if await _race_old_upserts(engine):
            return
    pytest.skip("the race did not bite on this machine; the error-free test is unproven here")


# --- single run ----------------------------------------------------------------

async def test_a_second_run_cannot_start_while_one_holds_the_lock(engine, seeded):
    async with rk._single_run_lock(engine):
        args = argparse.Namespace(mode="expand", batch_size=2, pre_window=False)
        assert await rk._run(args, engine) == EXIT_FAILED
    # nothing was added by the refused run
    assert await _scalar(
        engine, "SELECT count(*) FROM information_schema.columns WHERE table_name='tracks' AND column_name='region'"
    ) == 0
    async with rk._single_run_lock(engine):  # released once the holder is gone
        pass



async def test_each_step_logs_its_elapsed_time(engine, seeded, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(rk.logger, "info", lambda msg, *a, extra=None, **k: seen.append({"msg": msg, **(extra or {})}))
    await _full_cycle(engine)
    await rk.finalize(engine, _Stop(), 2)
    steps = {e["step"] for e in seen if e["msg"] == "RegionKeys: step done"}
    assert {"expand (total)", "backfill author_book.book_region", "build uq_books_asin_region", "finalize (total)"} <= steps
    assert all("elapsed_s" in e for e in seen if e["msg"] == "RegionKeys: step done")
