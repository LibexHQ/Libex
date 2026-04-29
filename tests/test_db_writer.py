"""
DB writer service unit tests.
Tests upsert_author null-asin upgrade logic and race condition handling.
All DB interactions are mocked — we test our logic not SQLAlchemy.
"""

# Standard library
from unittest.mock import AsyncMock, MagicMock, call, patch

# Third party
import pytest
from sqlalchemy.exc import IntegrityError

# Local
from app.services.db.writer import upsert_author


# ============================================================
# HELPERS
# ============================================================

def _make_session(null_id=None, insert_id=None):
    """
    Builds a mock AsyncSession.

    null_id:   scalar returned by the null-asin SELECT (Author.id or None)
    insert_id: scalar returned by the INSERT ... RETURNING id
    """
    session = AsyncMock()

    null_result = MagicMock()
    null_result.scalar_one_or_none.return_value = null_id

    insert_result = MagicMock()
    insert_result.fetchone.return_value = (insert_id,) if insert_id is not None else None

    # First execute call → null-asin SELECT
    # Second execute call → UPDATE or INSERT
    session.execute = AsyncMock(side_effect=[null_result, insert_result])
    return session


# ============================================================
# NULL-ASIN UPGRADE — HAPPY PATH
# ============================================================

@pytest.mark.asyncio
async def test_upsert_author_upgrades_null_asin_row():
    """When a null-asin row exists, it is upgraded with the real ASIN."""
    session = _make_session(null_id=42)
    result = await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    assert result == 42


@pytest.mark.asyncio
async def test_upsert_author_upgrade_calls_update_not_insert():
    """Null-asin upgrade path issues an UPDATE not a second INSERT."""
    session = _make_session(null_id=42)
    await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    # Two execute calls: SELECT then UPDATE
    assert session.execute.call_count == 2


@pytest.mark.asyncio
async def test_upsert_author_upgrade_returns_existing_id():
    """Null-asin upgrade returns the existing row id, not a new one."""
    session = _make_session(null_id=99)
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
    When a concurrent request already upgraded the null-asin row,
    IntegrityError is caught and the existing id is returned.
    """
    session = AsyncMock()

    null_result = MagicMock()
    null_result.scalar_one_or_none.return_value = 42

    session.execute = AsyncMock(side_effect=[
        null_result,
        IntegrityError("duplicate", {}, Exception()),
    ])
    session.rollback = AsyncMock()

    result = await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    assert result == 42


@pytest.mark.asyncio
async def test_upsert_author_upgrade_race_condition_calls_rollback():
    """IntegrityError during upgrade triggers session rollback."""
    session = AsyncMock()

    null_result = MagicMock()
    null_result.scalar_one_or_none.return_value = 42

    session.execute = AsyncMock(side_effect=[
        null_result,
        IntegrityError("duplicate", {}, Exception()),
    ])
    session.rollback = AsyncMock()

    await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    session.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_upsert_author_upgrade_race_condition_does_not_reraise():
    """IntegrityError during upgrade is swallowed — does not propagate."""
    session = AsyncMock()

    null_result = MagicMock()
    null_result.scalar_one_or_none.return_value = 42

    session.execute = AsyncMock(side_effect=[
        null_result,
        IntegrityError("duplicate", {}, Exception()),
    ])
    session.rollback = AsyncMock()

    # Should not raise
    result = await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    assert result is not None


# ============================================================
# NO NULL-ASIN ROW — STANDARD UPSERT
# ============================================================

@pytest.mark.asyncio
async def test_upsert_author_with_asin_no_null_row_inserts():
    """When no null-asin row exists, standard INSERT ... RETURNING is used."""
    session = _make_session(null_id=None, insert_id=7)
    result = await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    assert result == 7


@pytest.mark.asyncio
async def test_upsert_author_with_asin_no_null_row_two_executes():
    """Standard path issues SELECT then INSERT — two execute calls."""
    session = _make_session(null_id=None, insert_id=7)
    await upsert_author(session, {
        "asin": "B000APHM1K",
        "name": "Vince Flynn",
        "region": "us",
    })
    assert session.execute.call_count == 2


# ============================================================
# NULL-ASIN INSERT PATH
# ============================================================

@pytest.mark.asyncio
async def test_upsert_author_null_asin_existing_row_returns_id():
    """When author has no ASIN and a matching null-asin row exists, returns existing id."""
    session = AsyncMock()
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = 55
    session.execute = AsyncMock(return_value=existing_result)

    result = await upsert_author(session, {
        "asin": None,
        "name": "Vince Flynn",
        "region": "us",
    })
    assert result == 55


@pytest.mark.asyncio
async def test_upsert_author_null_asin_existing_row_no_insert():
    """When null-asin row already exists, no INSERT is issued."""
    session = AsyncMock()
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = 55
    session.execute = AsyncMock(return_value=existing_result)

    await upsert_author(session, {
        "asin": None,
        "name": "Vince Flynn",
        "region": "us",
    })
    # Only one execute: the SELECT
    assert session.execute.call_count == 1


@pytest.mark.asyncio
async def test_upsert_author_null_asin_new_row_inserts():
    """When no null-asin row exists and no ASIN, inserts new row."""
    session = AsyncMock()

    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = None

    insert_result = MagicMock()
    insert_result.fetchone.return_value = (88,)

    session.execute = AsyncMock(side_effect=[select_result, insert_result])

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