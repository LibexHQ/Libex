"""
DB writer service unit tests.
Tests upsert_author null-asin upgrade logic and race condition handling.
All DB interactions are mocked — we test our logic not SQLAlchemy.
"""

# Standard library
from unittest.mock import AsyncMock, MagicMock

# Third party
import pytest
from sqlalchemy.exc import IntegrityError

# Local
from app.services.db.writer import upsert_author


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