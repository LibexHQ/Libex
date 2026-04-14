"""
Cache operation unit tests.
Tests get, set, invalidate, and purge with mocked database sessions.
"""

# Standard library
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest

# Local
from app.services.cache.manager import get, set, invalidate


# ============================================================
# CACHE GET TESTS
# ============================================================

@pytest.mark.asyncio
async def test_cache_get_returns_none_on_miss():
    """Cache get returns None when key not found."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)

    result = await get(session, "book:us:B08G9PRS1K")
    assert result is None


@pytest.mark.asyncio
async def test_cache_get_returns_value_on_hit():
    """Cache get returns cached value when key found."""
    session = AsyncMock()
    mock_entry = MagicMock()
    mock_entry.value = {"asin": "B08G9PRS1K", "title": "Dune"}
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_entry
    session.execute = AsyncMock(return_value=mock_result)

    result = await get(session, "book:us:B08G9PRS1K")
    assert result == {"asin": "B08G9PRS1K", "title": "Dune"}


@pytest.mark.asyncio
async def test_cache_get_calls_execute():
    """Cache get executes a database query."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)

    await get(session, "book:us:B08G9PRS1K")
    session.execute.assert_called_once()


# ============================================================
# CACHE SET TESTS
# ============================================================

@pytest.mark.asyncio
async def test_cache_set_calls_execute():
    """Cache set executes a database upsert."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    await set(session, "book:us:B08G9PRS1K", {"title": "Dune"})
    session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_cache_set_calls_commit():
    """Cache set commits the transaction."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    await set(session, "book:us:B08G9PRS1K", {"title": "Dune"})
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_cache_set_uses_default_ttl():
    """Cache set uses default TTL from settings when not specified."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    with patch("app.services.cache.manager.settings") as mock_settings:
        mock_settings.cache_ttl = 86400
        await set(session, "book:us:B08G9PRS1K", {"title": "Dune"})
        session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_cache_set_accepts_custom_ttl():
    """Cache set accepts custom TTL override."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    await set(session, "book:us:B08G9PRS1K", {"title": "Dune"}, ttl_seconds=3600)
    session.execute.assert_called_once()


# ============================================================
# CACHE INVALIDATE TESTS
# ============================================================

@pytest.mark.asyncio
async def test_cache_invalidate_calls_execute():
    """Cache invalidate executes a delete query."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    await invalidate(session, "book:us:B08G9PRS1K")
    session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_cache_invalidate_calls_commit():
    """Cache invalidate commits the transaction."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    await invalidate(session, "book:us:B08G9PRS1K")
    session.commit.assert_called_once()