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
from sqlalchemy.exc import IntegrityError

# Local
from app.services.db.writer import (
    upsert_author,
    reconcile_genres,
    persist_author_books_cache_background,
)


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
async def test_upsert_author_upgrade_race_condition_calls_rollback():
    """IntegrityError during upgrade triggers session rollback."""
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
    session.rollback.assert_called_once()


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
# insert path catches that, rolls back, and returns the row the winner inserted.

@pytest.mark.asyncio
async def test_upsert_author_null_asin_insert_conflict_returns_winner_id():
    """
    When the null-asin insert hits the partial-index conflict, it rolls back and
    returns the id of the row the concurrent winner inserted.
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
async def test_upsert_author_null_asin_insert_conflict_calls_rollback():
    """A conflict on the null-asin insert triggers a session rollback."""
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
    session.rollback.assert_awaited_once()


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


def _fake_cache_row(value):
    """
    Stands in for the Cache row `SELECT ... FOR UPDATE` locks and reads
    back via `.scalar_one_or_none()` — carries exactly the two attributes
    the guard reads off it (`.value`, `.expires_at`), unexpired by default.
    """
    return SimpleNamespace(value=value, expires_at=datetime.now(timezone.utc) + timedelta(hours=1))


async def _run_persist_author_books_cache_background(key, asins, stored):
    """
    Drives persist_author_books_cache_background's background task to
    completion: create_task is intercepted so its coroutine is awaited
    directly, and _BackgroundSession / cache.set are patched at the module
    the function reads them from. session.execute(...).scalar_one_or_none()
    is stubbed directly (the guard now takes a `SELECT ... FOR UPDATE` on
    the Cache row instead of calling cache.get) to return a fake row
    carrying `stored`, or None when nothing is stored yet. Returns the
    mocked cache.set so callers can inspect what — or whether — it was
    written.
    """
    mock_session = AsyncMock()
    row = _fake_cache_row(stored) if stored is not None else None
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
        persist_author_books_cache_background(key, asins)
        await captured["coro"]

    return mock_set


@pytest.mark.asyncio
async def test_persist_author_books_cache_background_refuses_shorter_list():
    """A strictly shorter incoming list than what's stored is refused."""
    mock_set = await _run_persist_author_books_cache_background(
        "author_books:us:B000AUTHOR", ["B0ASIN0001"], stored=["B0ASIN0001", "B0ASIN0002"],
    )
    mock_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_author_books_cache_background_refuses_strict_subset():
    """A same-length-or-longer incoming list that is still a strict subset of
    what's stored is refused — length alone isn't the whole check."""
    mock_set = await _run_persist_author_books_cache_background(
        "author_books:us:B000AUTHOR",
        ["B0ASIN0001", "B0ASIN0001"],
        stored=["B0ASIN0001", "B0ASIN0002"],
    )
    mock_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_author_books_cache_background_refuses_empty_against_non_empty():
    """An empty incoming list against a non-empty stored one is refused."""
    mock_set = await _run_persist_author_books_cache_background(
        "author_books:us:B000AUTHOR", [], stored=["B0ASIN0001"],
    )
    mock_set.assert_not_awaited()


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