"""
DB writer service unit tests.
Tests upsert_author null-asin upgrade logic and race condition handling.
All DB interactions are mocked — we test our logic not SQLAlchemy.
"""

# Standard library
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

# Local
from app.core.config import get_settings
from app.db.models import Book
from app.services.db.writer import (
    _PERSIST_CHUNK_SIZE,
    _longer_wins,
    upsert_author,
    upsert_book,
    reconcile_genres,
    persist_author_books_cache_background,
)

settings = get_settings()


# ============================================================
# HELPERS
# ============================================================

def _scalar(value):
    """Returns a mock result whose scalar_one_or_none returns value."""
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _fetchone(id_):
    """Returns a mock result whose fetchone returns (id_,)."""
    r = MagicMock()
    r.fetchone.return_value = (id_,) if id_ is not None else None
    return r


def _session(*side_effects):
    """Builds a mock AsyncSession with the given execute side_effects."""
    s = AsyncMock()
    s.execute = AsyncMock(side_effect=list(side_effects))
    s.rollback = AsyncMock()
    return s


# ============================================================
# EXISTING ASIN ROW — SHORT CIRCUIT (step 1)
# ============================================================

@pytest.mark.asyncio
async def test_upsert_author_returns_existing_id_when_asin_row_exists():
    """When fully-upgraded row already exists, returns its id immediately."""
    session = _session(_scalar(42))
    result = await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    assert result == 42


@pytest.mark.asyncio
async def test_upsert_author_only_one_execute_when_asin_row_exists():
    """Short-circuit path issues only one SELECT — no UPDATE or INSERT."""
    session = _session(_scalar(42))
    await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    assert session.execute.call_count == 1


@pytest.mark.asyncio
async def test_upsert_author_no_rollback_on_short_circuit():
    """Short-circuit path never calls rollback."""
    session = _session(_scalar(42))
    await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    session.rollback.assert_not_called()


# ============================================================
# NULL-ASIN UPGRADE — HAPPY PATH (step 2)
# ============================================================

@pytest.mark.asyncio
async def test_upsert_author_upgrades_null_asin_row():
    """When no upgraded row exists but a null-asin row does, it is upgraded."""
    session = _session(_scalar(None), _scalar(42), MagicMock())
    result = await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    assert result == 42


@pytest.mark.asyncio
async def test_upsert_author_upgrade_three_executes():
    """Upgrade path issues three execute calls: existing SELECT, null SELECT, UPDATE."""
    session = _session(_scalar(None), _scalar(42), MagicMock())
    await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    assert session.execute.call_count == 3


@pytest.mark.asyncio
async def test_upsert_author_upgrade_returns_null_row_id():
    """Upgrade returns the null-asin row's id, not a newly generated one."""
    session = _session(_scalar(None), _scalar(99), MagicMock())
    result = await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
        "image": "https://example.com/img.jpg",
    })
    assert result == 99


# ============================================================
# NULL-ASIN UPGRADE — RACE CONDITION
# ============================================================

@pytest.mark.asyncio
async def test_upsert_author_upgrade_race_condition_returns_id():
    """
    When a concurrent request upgraded between our SELECT and UPDATE,
    IntegrityError is caught and the null row id is returned.
    """
    session = _session(
        _scalar(None),
        _scalar(42),
        IntegrityError("duplicate", {}, Exception()),
    )
    result = await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    assert result == 42


@pytest.mark.asyncio
async def test_upsert_author_upgrade_race_condition_rolls_back_to_savepoint():
    """IntegrityError during upgrade rolls the failed UPDATE back to its SAVEPOINT."""
    session = _session(
        _scalar(None),
        _scalar(42),
        IntegrityError("duplicate", {}, Exception()),
    )
    await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    session.begin_nested.return_value.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_author_upgrade_race_condition_spares_the_transaction():
    """
    Losing the upgrade race undoes the failed UPDATE and nothing else. A
    session-wide rollback here would discard whatever else the caller has
    written in the same transaction — a batched book persist writes fifty
    books before it commits — and would do it silently, since the race is
    swallowed and never reaches the caller.
    """
    session = _session(
        _scalar(None),
        _scalar(42),
        IntegrityError("duplicate", {}, Exception()),
    )
    await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    session.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_author_upgrade_race_condition_does_not_reraise():
    """IntegrityError during upgrade is swallowed — does not propagate."""
    session = _session(
        _scalar(None),
        _scalar(42),
        IntegrityError("duplicate", {}, Exception()),
    )
    result = await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    assert result is not None


# ============================================================
# NO EXISTING ROWS — STANDARD UPSERT (step 3)
# ============================================================

@pytest.mark.asyncio
async def test_upsert_author_with_asin_no_existing_rows_inserts():
    """When neither upgraded nor null-asin row exists, INSERT is used."""
    session = _session(_scalar(None), _scalar(None), _fetchone(7))
    result = await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    assert result == 7


@pytest.mark.asyncio
async def test_upsert_author_with_asin_no_existing_rows_three_executes():
    """Standard insert path issues three execute calls: two SELECTs then INSERT."""
    session = _session(_scalar(None), _scalar(None), _fetchone(7))
    await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    assert session.execute.call_count == 3


# ============================================================
# NULL-ASIN INSERT PATH
# ============================================================

@pytest.mark.asyncio
async def test_upsert_author_null_asin_existing_row_returns_id():
    """When author has no ASIN and a matching null-asin row exists, returns id."""
    session = _session(_scalar(55))
    result = await upsert_author(session, {
        "asin": None,
        "name": "Vince Flynn",
        "region": "us",
    })
    assert result == 55


@pytest.mark.asyncio
async def test_upsert_author_null_asin_existing_row_one_execute():
    """Existing null-asin path issues only one SELECT."""
    session = _session(_scalar(55))
    await upsert_author(session, {
        "asin": None,
        "name": "Vince Flynn",
        "region": "us",
    })
    assert session.execute.call_count == 1


@pytest.mark.asyncio
async def test_upsert_author_null_asin_new_row_inserts():
    """When no null-asin row exists and no ASIN, inserts new row."""
    session = _session(_scalar(None), _fetchone(88))
    result = await upsert_author(session, {
        "asin": None,
        "name": "New Author",
        "region": "us",
    })
    assert result == 88


# ============================================================
# NULL-ASIN LOOKUPS — DUPLICATE TOLERANCE
# ============================================================
# The unique constraint doesn't cover null asins (Postgres treats NULLs as
# distinct), so a concurrent-write race can leave two null-asin rows for the
# same (name, region). The lookups order by id and take the oldest so every
# writer converges on one row instead of raising MultipleResultsFound.

def _compiled(stmt) -> str:
    """Compiles a statement to SQL with literals inlined, for substring checks."""
    return str(stmt.compile(compile_kwargs={"literal_binds": True})).upper()


@pytest.mark.asyncio
async def test_upsert_author_upgrade_lookup_orders_and_limits():
    """The step-2 null-asin lookup takes the oldest row: ORDER BY id LIMIT 1."""
    session = _session(_scalar(None), _scalar(42), MagicMock())
    await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    # second execute is the null-asin lookup
    null_lookup = session.execute.call_args_list[1].args[0]
    sql = _compiled(null_lookup)
    assert "ORDER BY" in sql
    assert "LIMIT" in sql


@pytest.mark.asyncio
async def test_upsert_author_no_asin_lookup_orders_and_limits():
    """The no-asin branch's existing-row lookup also takes the oldest row."""
    session = _session(_scalar(55))
    await upsert_author(session, {
        "asin": None,
        "name": "Vince Flynn",
        "region": "us",
    })
    # first (only) execute is the null-asin lookup
    lookup = session.execute.call_args_list[0].args[0]
    sql = _compiled(lookup)
    assert "ORDER BY" in sql
    assert "LIMIT" in sql


@pytest.mark.asyncio
async def test_upsert_author_upgrade_lookup_still_filters_null_asin():
    """Duplicate tolerance didn't change what the lookup matches: null-asin only."""
    session = _session(_scalar(None), _scalar(42), MagicMock())
    await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    null_lookup = session.execute.call_args_list[1].args[0]
    sql = _compiled(null_lookup)
    assert "ASIN IS NULL" in sql


# ============================================================
# NULL-ASIN INSERT — PARTIAL-INDEX CONFLICT HANDLING
# ============================================================
# A partial unique index on (name, region) WHERE asin IS NULL means a concurrent
# insert of the same null-asin author conflicts instead of duplicating. The
# insert path catches that, rolls the INSERT back to its own SAVEPOINT, and
# returns the row the winner inserted.

@pytest.mark.asyncio
async def test_upsert_author_null_asin_insert_conflict_returns_winner_id():
    """
    When the null-asin insert hits the partial-index conflict, it undoes the
    INSERT and returns the id of the row the concurrent winner inserted.
    """
    session = _session(
        _scalar(None),                               # initial lookup: no row yet
        IntegrityError("duplicate", {}, Exception()),  # insert loses the race
        _scalar(42),                                 # reselect finds the winner
    )
    result = await upsert_author(session, {
        "asin": None,
        "name": "Racey Author",
        "region": "us",
    })
    assert result == 42


@pytest.mark.asyncio
async def test_upsert_author_null_asin_insert_conflict_rolls_back_to_savepoint():
    """A conflict on the null-asin insert rolls that INSERT back to its SAVEPOINT."""
    session = _session(
        _scalar(None),
        IntegrityError("duplicate", {}, Exception()),
        _scalar(42),
    )
    await upsert_author(session, {
        "asin": None,
        "name": "Racey Author",
        "region": "us",
    })
    session.begin_nested.return_value.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_author_null_asin_insert_conflict_spares_the_transaction():
    """
    Same guarantee as the upgrade path: the loser of the insert race undoes
    its own INSERT and leaves the rest of the caller's transaction standing.
    """
    session = _session(
        _scalar(None),
        IntegrityError("duplicate", {}, Exception()),
        _scalar(42),
    )
    await upsert_author(session, {
        "asin": None,
        "name": "Racey Author",
        "region": "us",
    })
    session.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_author_null_asin_insert_conflict_does_not_reraise():
    """A conflict on the null-asin insert is swallowed — it does not propagate."""
    session = _session(
        _scalar(None),
        IntegrityError("duplicate", {}, Exception()),
        _scalar(99),
    )
    # should not raise
    result = await upsert_author(session, {
        "asin": None,
        "name": "Racey Author",
        "region": "us",
    })
    assert result == 99


# ============================================================
# GUARD CLAUSES
# ============================================================

@pytest.mark.asyncio
async def test_upsert_author_missing_name_returns_none():
    """Author with no name returns None without hitting DB."""
    session = AsyncMock()
    result = await upsert_author(session, {"asin": "B000APHM1K", "region": "us"})
    assert result is None
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_author_missing_region_returns_none():
    """Author with no region returns None without hitting DB."""
    session = AsyncMock()
    result = await upsert_author(session, {"asin": "B000APHM1K", "name": "Vince Flynn"})
    assert result is None
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_author_empty_name_returns_none():
    """Author with empty name string returns None without hitting DB."""
    session = AsyncMock()
    result = await upsert_author(session, {"asin": "B000APHM1K", "name": "  ", "region": "us"})
    assert result is None
    session.execute.assert_not_called()


# ============================================================
# reconcile_genres — upsert then prune
# ============================================================

def _delete_capturing_session(*side_effects):
    """
    AsyncSession that records every executed statement, so a test can tell the
    per-node upserts apart from the final prune delete.
    """
    s = AsyncMock()
    s.executed = []

    async def _execute(stmt, *a, **kw):
        s.executed.append(stmt)
        return MagicMock()

    s.execute = AsyncMock(side_effect=_execute)
    s.rollback = AsyncMock()
    return s


@pytest.mark.asyncio
async def test_reconcile_genres_empty_list_noops():
    """An empty genre list writes nothing."""
    session = _delete_capturing_session()
    await reconcile_genres(session, "us", [])
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_genres_upserts_every_node_then_prunes():
    """
    reconcile_genres issues one execute per node (the upserts) plus one final
    execute (the prune delete) — so three nodes means four executes.
    """
    session = _delete_capturing_session()
    genres = [
        {"genre_id": "P1", "parent_id": "", "name": "Arts"},
        {"genre_id": "C1", "parent_id": "P1", "name": "Performing"},
        {"genre_id": "G1", "parent_id": "C1", "name": "Film"},
    ]
    await reconcile_genres(session, "us", genres)
    # 3 upserts + 1 prune delete
    assert session.execute.call_count == 4


@pytest.mark.asyncio
async def test_reconcile_genres_last_statement_is_a_delete():
    """The final statement reconcile issues is a DELETE (the prune)."""
    session = _delete_capturing_session()
    genres = [
        {"genre_id": "P1", "parent_id": "", "name": "Arts"},
        {"genre_id": "C1", "parent_id": "P1", "name": "Performing"},
    ]
    await reconcile_genres(session, "us", genres)
    last = session.executed[-1]
    # the prune is a Delete construct; the upserts before it are Inserts
    assert last.__class__.__name__ == "Delete"


@pytest.mark.asyncio
async def test_reconcile_genres_prune_filters_to_region_and_fresh_keys():
    """
    The prune delete is scoped to the region and excludes the fresh keys — its
    compiled SQL names the region and carries a NOT IN over (genre_id, parent_id).
    """
    session = _delete_capturing_session()
    genres = [
        {"genre_id": "P1", "parent_id": "", "name": "Arts"},
        {"genre_id": "C1", "parent_id": "P1", "name": "Performing"},
    ]
    await reconcile_genres(session, "us", genres)
    delete_stmt = session.executed[-1]
    sql = str(delete_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "catalog_genres" in sql
    assert "NOT IN" in sql.upper()
    # both fresh ids appear in the keep-set of the NOT IN
    assert "P1" in sql and "C1" in sql


# ============================================================
# persist_author_books_cache_background — shrink-refusal backstop
# ============================================================
# The function fires a background asyncio task via create_task; these tests
# intercept create_task and await the same coroutine directly instead of
# letting it run detached, so the write-side logic itself — not just that a
# task got scheduled — is under test.

class _FakeSessionCM:
    """An async context manager standing in for `_BackgroundSession()`."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        return False


def _fake_cache_row(value, expires_at=None):
    """
    Stands in for the Cache row `SELECT ... FOR UPDATE` locks and reads
    back via `.scalar_one_or_none()` — carries exactly the two attributes
    the guard reads off it (`.value`, `.expires_at`), unexpired by default
    (now + 1h) unless the caller hands a specific expires_at to drive the
    keep-later-expiry rule.
    """
    return SimpleNamespace(
        value=value,
        expires_at=expires_at if expires_at is not None else datetime.now(timezone.utc) + timedelta(hours=1),
    )


async def _run_persist_author_books_cache_background(
    key, asins, stored, stored_expires_at=None, ttl_seconds=None, frozen_now=None,
):
    """
    Drives persist_author_books_cache_background's background task to
    completion: create_task is intercepted so its coroutine is awaited
    directly, and _BackgroundSession / cache.set are patched at the module
    the function reads them from. session.execute(...).scalar_one_or_none()
    is stubbed directly (the guard now takes a `SELECT ... FOR UPDATE` on
    the Cache row instead of calling cache.get) to return a fake row
    carrying `stored`, or None when nothing is stored yet. stored_expires_at
    lets a caller drive the keep-later-expiry rule with a specific stored
    row expiry instead of the unexpired-by-default one hour out.
    frozen_now, when given, pins the module's own _now() so the expiry
    arithmetic in the keep-later comparison is exact rather than off by
    however long the test happened to take to run. Returns the mocked
    cache.set so callers can inspect what -- or whether -- it was written.
    """
    mock_session = AsyncMock()
    row = _fake_cache_row(stored, expires_at=stored_expires_at) if stored is not None else None
    mock_session.execute = AsyncMock(return_value=_scalar(row))
    captured = {}

    def _fake_create_task(coro):
        captured["coro"] = coro
        return MagicMock()

    # A fresh semaphore scoped to this call, not the module-global one: the
    # global is bound lazily to whichever event loop first uses it, and
    # pytest-asyncio hands each test function its own loop, so sharing the
    # module-global across tests would raise on the second test to reach it.
    with patch("app.services.db.writer._BackgroundSession", return_value=_FakeSessionCM(mock_session)), \
         patch("app.services.db.writer._bg_write_semaphore", asyncio.Semaphore(2)), \
         patch("app.services.db.writer.asyncio.create_task", side_effect=_fake_create_task), \
         patch("app.services.cache.manager.set", new=AsyncMock()) as mock_set:
        if frozen_now is not None:
            with patch("app.services.db.writer._now", return_value=frozen_now):
                persist_author_books_cache_background(key, asins, ttl_seconds=ttl_seconds)
                await captured["coro"]
        else:
            persist_author_books_cache_background(key, asins, ttl_seconds=ttl_seconds)
            await captured["coro"]

    return mock_set


@pytest.mark.asyncio
async def test_persist_author_books_cache_background_shorter_list_unions_in_the_missing_member():
    """A strictly shorter incoming list never drops a stored member — the
    write is the union, incoming first, with the stored-only ASIN appended."""
    mock_set = await _run_persist_author_books_cache_background(
        "author_books:us:B000AUTHOR", ["B0ASIN0001"], stored=["B0ASIN0001", "B0ASIN0002"],
    )
    mock_set.assert_awaited_once()
    written_value = mock_set.await_args.args[2]
    assert written_value == ["B0ASIN0001", "B0ASIN0002"]


@pytest.mark.asyncio
async def test_persist_author_books_cache_background_strict_subset_unions_in_the_missing_member():
    """A same-length-or-longer incoming list that is still a strict subset of
    what's stored still retains every stored member via the union — length
    alone was never the whole check, and it still isn't."""
    mock_set = await _run_persist_author_books_cache_background(
        "author_books:us:B000AUTHOR",
        ["B0ASIN0001", "B0ASIN0001"],
        stored=["B0ASIN0001", "B0ASIN0002"],
    )
    mock_set.assert_awaited_once()
    written_value = mock_set.await_args.args[2]
    assert written_value == ["B0ASIN0001", "B0ASIN0001", "B0ASIN0002"]


@pytest.mark.asyncio
async def test_persist_author_books_cache_background_empty_incoming_unions_in_all_of_stored():
    """An empty incoming list against a non-empty stored one still retains
    every stored member — the union of nothing-new with what's stored is
    just what's stored, appended after the (empty) incoming order."""
    mock_set = await _run_persist_author_books_cache_background(
        "author_books:us:B000AUTHOR", [], stored=["B0ASIN0001"],
    )
    mock_set.assert_awaited_once()
    written_value = mock_set.await_args.args[2]
    assert written_value == ["B0ASIN0001"]


@pytest.mark.asyncio
async def test_persist_author_books_cache_background_equal_length_swap_retains_dropped_member():
    """The old guard (`len(asins) < len(stored) or incoming_set < stored_set`)
    let an equal-length swap through unconditionally and lost the member the
    swap dropped: stored=['A','B','C'], incoming=['A','B','D'] silently lost
    'C'. The union must retain it."""
    mock_set = await _run_persist_author_books_cache_background(
        "author_books:us:B000AUTHOR",
        ["B0ASIN0001", "B0ASIN0002", "B0ASIN0004"],
        stored=["B0ASIN0001", "B0ASIN0002", "B0ASIN0003"],
    )
    mock_set.assert_awaited_once()
    written_value = mock_set.await_args.args[2]
    assert written_value == ["B0ASIN0001", "B0ASIN0002", "B0ASIN0004", "B0ASIN0003"]


@pytest.mark.asyncio
async def test_persist_author_books_cache_background_writes_when_nothing_stored():
    """When nothing is stored yet, the incoming list is written unconditionally."""
    mock_set = await _run_persist_author_books_cache_background(
        "author_books:us:B000AUTHOR", ["B0ASIN0001"], stored=None,
    )
    mock_set.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_author_books_cache_background_writes_on_same_length_member_swap():
    """A same-length list whose members differ from what's stored (a swap,
    not a shrink) is written normally."""
    mock_set = await _run_persist_author_books_cache_background(
        "author_books:us:B000AUTHOR",
        ["B0ASIN0003", "B0ASIN0002"],
        stored=["B0ASIN0001", "B0ASIN0002"],
    )
    mock_set.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_author_books_cache_background_persists_exact_order():
    """The incoming list is persisted in exactly the order it was handed,
    with no sorting or reordering applied."""
    incoming = ["B0ASIN0003", "B0ASIN0001", "B0ASIN0002"]
    mock_set = await _run_persist_author_books_cache_background(
        "author_books:us:B000AUTHOR", incoming, stored=None,
    )
    mock_set.assert_awaited_once()
    written_key = mock_set.await_args.args[1]
    written_value = mock_set.await_args.args[2]
    assert written_key == "author_books:us:B000AUTHOR"
    assert written_value == incoming


# ============================================================
# persist_author_books_cache_background — keep-later expiry
# ============================================================
# A plain overwrite of expires_at with whatever this call's own ttl_seconds
# implies would let a degraded (short-TTL) write clobber an already-earned
# long window down to the short one -- these pin that the write keeps
# whichever expiry is LATER, the stored row's or the one this call is
# asking for, in both directions.

@pytest.mark.asyncio
async def test_persist_author_books_cache_background_degraded_write_does_not_shorten_earned_long_window():
    """A stored row with ~20000s left on an already-earned long window,
    written again with the short degraded TTL (900s, matching
    AUTHOR_BOOKS_DEGRADED_CACHE_TTL_SECONDS), must keep the LATER, longer
    expiry rather than being clobbered down to 900s -- a plain overwrite
    (the pre-fix behavior) would apply the incoming 900s unconditionally
    regardless of what's already been earned."""
    frozen_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stored_expires_at = frozen_now + timedelta(seconds=20000)

    mock_set = await _run_persist_author_books_cache_background(
        "author_books:us:B000AUTHOR", ["B0ASIN0001"], stored=["B0ASIN0001"],
        stored_expires_at=stored_expires_at, ttl_seconds=900, frozen_now=frozen_now,
    )

    mock_set.assert_awaited_once()
    written_ttl = mock_set.await_args.kwargs["ttl_seconds"]
    assert written_ttl == 20000


@pytest.mark.asyncio
async def test_persist_author_books_cache_background_long_write_still_extends_a_shorter_stored_window():
    """Complement to the above: a stored row with only 300s left, written
    with the default day-long TTL (ttl_seconds=None -> settings.cache_ttl),
    must extend all the way out to the new, later expiry -- the keep-later
    rule must not freeze the window at whatever was first earned, only
    refuse to shorten it."""
    frozen_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stored_expires_at = frozen_now + timedelta(seconds=300)

    mock_set = await _run_persist_author_books_cache_background(
        "author_books:us:B000AUTHOR", ["B0ASIN0001"], stored=["B0ASIN0001"],
        stored_expires_at=stored_expires_at, ttl_seconds=None, frozen_now=frozen_now,
    )

    mock_set.assert_awaited_once()
    written_ttl = mock_set.await_args.kwargs["ttl_seconds"]
    assert written_ttl == settings.cache_ttl


@pytest.mark.asyncio
async def test_persist_author_books_cache_background_no_stored_row_uses_requested_ttl_outright():
    """With nothing stored to compare against, there is no later expiry to
    keep -- the requested ttl_seconds (or the default) is used as-is, same
    as a first write always has been."""
    frozen_now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    mock_set = await _run_persist_author_books_cache_background(
        "author_books:us:B000AUTHOR", ["B0ASIN0001"], stored=None,
        ttl_seconds=900, frozen_now=frozen_now,
    )

    mock_set.assert_awaited_once()
    written_ttl = mock_set.await_args.kwargs["ttl_seconds"]
    assert written_ttl == 900


# ============================================================
# _longer_wins — THE NULL-LENGTH TRAP
# ============================================================
# length(NULL) is NULL and every comparison against NULL is NULL rather than
# false, so a length-vs-length test alone silently resolves to its ELSE branch
# whenever the stored value is NULL. These check the compiled SQL carries the
# guards that stop that; the behaviour they produce against real Postgres is
# proved end to end in tests/integration/test_db_longer_wins.py.

def _longer_wins_sql(new_value="incoming text") -> str:
    """Compiles _longer_wins against a real text column for substring checks."""
    return _compiled(select(_longer_wins(new_value, Book.summary)))


def test_longer_wins_floors_the_stored_length_so_null_can_be_filled():
    """The stored side is floored to a sentinel: without it, a NULL stored
    value makes the comparison NULL, the CASE takes ELSE, and the column can
    never be filled by any later value however long."""
    sql = _longer_wins_sql()
    assert "COALESCE(LENGTH(BOOKS.SUMMARY), -1)" in sql


def test_longer_wins_floors_the_incoming_length_too():
    """The incoming side is floored by the same sentinel, so a NULL incoming
    value measures as absent and loses to anything stored rather than making
    the comparison NULL by a different route."""
    sql = _longer_wins_sql()
    assert "COALESCE(LENGTH(NULLIF(BTRIM(" in sql
    assert "), -1) >" in sql


def test_longer_wins_measures_the_incoming_value_content_free_trimmed():
    """An incoming empty or whitespace-only value is measured as absent, so it
    cannot displace a stored NULL. It carries no more information than NULL
    and would make a never-answered column look like a blank-answered one."""
    sql = _longer_wins_sql("   ")
    assert "NULLIF(BTRIM('   '), '')" in sql


def test_longer_wins_writes_the_incoming_value_untrimmed():
    """Only the measurement is trimmed. The value written is the value
    received, so real text keeps whatever spacing Audible sent."""
    sql = _longer_wins_sql("  padded real text  ")
    assert "THEN '  PADDED REAL TEXT  '" in sql


def test_longer_wins_keeps_the_existing_column_in_the_else_branch():
    """The fallback is still the stored column, so losing the comparison for
    any reason keeps what is already held rather than writing NULL."""
    assert "ELSE BOOKS.SUMMARY" in _longer_wins_sql()


@pytest.mark.parametrize("column", ["DESCRIPTION", "SUMMARY"])
def test_upsert_book_uses_the_guarded_comparison_for_its_text_columns(column):
    """Both of the book columns merged by length carry the guards, so neither
    can be pinned NULL by its first write."""
    session = _session(*[MagicMock() for _ in range(4)])
    asyncio.run(upsert_book(session, {
        "asin": "B0BEXAMPLE",
        "title": "Example",
        "region": "us",
        "description": "a description",
        "summary": "a summary",
    }))
    sql = _compiled(session.execute.call_args_list[0].args[0])
    guarded = f"COALESCE(LENGTH(BOOKS.{column}), -1)"
    assert guarded in sql


# ============================================================
# persist_books_background — CHUNK BOUNDARIES
# ============================================================
# The batched persist slices its book list into transactions of
# _PERSIST_CHUNK_SIZE. _persist_book_chunk is mocked out here so the slicing
# itself is what is measured -- what each chunk then does with a real
# database, including the replay after a lost transaction, is proved in
# tests/integration/test_persist_book_chunk.py.

async def _captured_chunks(books, region="us"):
    """
    Drives persist_books_background to completion and returns the chunks it
    handed to _persist_book_chunk, in order.

    create_task is intercepted and its coroutine awaited directly, so the
    slicing runs under the test rather than detached. The chunk lists are
    copied on capture: they are slices of the caller's list, and holding the
    live objects would let a later mutation rewrite what an earlier call is
    asserted to have received. The semaphore is replaced per call because the
    module-global binds to whichever event loop first uses it and each test
    gets its own.
    """
    from app.services.db.writer import persist_books_background

    captured_chunks = []
    captured_regions = []

    async def _record(session, chunk, chunk_region):
        captured_chunks.append(list(chunk))
        captured_regions.append(chunk_region)

    captured = {}

    def _fake_create_task(coro):
        captured["coro"] = coro
        return MagicMock()

    with patch("app.services.db.writer._BackgroundSession", return_value=_FakeSessionCM(AsyncMock())), \
         patch("app.services.db.writer._bg_write_semaphore", asyncio.Semaphore(2)), \
         patch("app.services.db.writer.asyncio.create_task", side_effect=_fake_create_task), \
         patch("app.services.db.writer._persist_book_chunk", side_effect=_record):
        persist_books_background(books, region)
        await captured["coro"]

    return captured_chunks, captured_regions


def _books(count, start=0):
    """count books with distinct ASINs, so a chunk's contents can be checked
    and not merely its length."""
    return [{"asin": f"B0BOOK{i:05d}", "title": f"Book {i}"} for i in range(start, start + count)]


@pytest.mark.asyncio
async def test_persist_books_background_splits_an_exact_multiple_evenly():
    """A list that is an exact multiple of the chunk size produces full
    chunks and no trailing empty one. An empty final chunk would reach
    Postgres as an INSERT with no rows on the cache write."""
    chunks, _ = await _captured_chunks(_books(_PERSIST_CHUNK_SIZE * 2))

    assert [len(c) for c in chunks] == [_PERSIST_CHUNK_SIZE, _PERSIST_CHUNK_SIZE]


@pytest.mark.asyncio
async def test_persist_books_background_carries_one_book_over_the_boundary():
    """One book past an exact multiple gets its own chunk rather than being
    dropped off the end -- the off-by-one that loses the tail of a prolific
    author's catalogue silently, since every other book still persists."""
    chunks, _ = await _captured_chunks(_books(_PERSIST_CHUNK_SIZE + 1))

    assert [len(c) for c in chunks] == [_PERSIST_CHUNK_SIZE, 1]


@pytest.mark.asyncio
async def test_persist_books_background_keeps_one_short_list_in_a_single_chunk():
    """One book under the size stays a single chunk, so the common case of a
    request smaller than the chunk size pays exactly one transaction."""
    chunks, _ = await _captured_chunks(_books(_PERSIST_CHUNK_SIZE - 1))

    assert [len(c) for c in chunks] == [_PERSIST_CHUNK_SIZE - 1]


@pytest.mark.asyncio
async def test_persist_books_background_writes_nothing_for_an_empty_list():
    """No books means no chunk at all, not one empty chunk."""
    chunks, _ = await _captured_chunks([])

    assert chunks == []


@pytest.mark.asyncio
async def test_persist_books_background_writes_nothing_when_no_book_has_an_asin():
    """A list whose books are all unusable collapses to no chunks, the same
    as an empty list -- the ASIN filter runs before the slicing, so an empty
    chunk cannot be produced by filtering either."""
    chunks, _ = await _captured_chunks([{"title": "No ASIN"}, {"asin": None, "title": "Null"}])

    assert chunks == []


@pytest.mark.asyncio
async def test_persist_books_background_chunks_cover_every_book_exactly_once():
    """The chunks reassemble into the original list, in order: nothing
    duplicated across a boundary and nothing skipped at one. Chunk lengths
    alone cannot see a slice that repeated or lost a book while still
    totalling correctly."""
    books = _books(_PERSIST_CHUNK_SIZE * 2 + 7)

    chunks, _ = await _captured_chunks(books)

    assert [book for chunk in chunks for book in chunk] == books


@pytest.mark.asyncio
async def test_persist_books_background_chunks_only_the_books_with_asins():
    """Books without an ASIN are filtered before the slicing, so they neither
    reach a chunk nor take up room in one. Chunking the unfiltered list would
    leave chunks that are short by however many were unusable."""
    books = _books(3) + [{"title": "No ASIN"}] + _books(2, start=3)

    chunks, _ = await _captured_chunks(books)

    assert [book for chunk in chunks for book in chunk] == _books(5)


@pytest.mark.asyncio
async def test_persist_books_background_passes_the_region_to_every_chunk():
    """Every chunk is written for the region the caller asked for. A chunk
    that lost it would key its cache entries under the wrong marketplace, and
    book ASINs are region-specific, so the entry would answer for a book that
    region does not have."""
    _, regions = await _captured_chunks(_books(_PERSIST_CHUNK_SIZE + 1), region="de")

    assert regions == ["de", "de"]


def test_persist_chunk_size_matches_the_audible_fetch_chunk():
    """The chunk size is 50 because that is the largest ASIN list a single
    Audible request takes. The two are coupled by intent rather than by a
    shared constant, which is the only reason it is pinned here."""
    assert _PERSIST_CHUNK_SIZE == 50
