"""
Service tests for the live new-releases and coming-soon endpoints.

Mocks Audible (audible_get) and the cache so we test the windowing, the
stop-condition paging, the cache-first short-circuit, and coming-soon's
soonest-first ordering — without real HTTP or DB.
"""

# Standard library
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Services
from app.services.audible import releases

NOW = datetime.now(timezone.utc)


def _product(asin, days_offset):
    """Builds a minimal Audible catalog product with a release date offset from now."""
    dt = NOW + timedelta(days=days_offset)
    return {
        "asin": asin,
        "title": f"Book {asin}",
        "release_date": dt.strftime("%Y-%m-%d"),
        "publication_datetime": dt.isoformat(),
    }


def _page(*products):
    return {"products": list(products)}


def _empty():
    return {"products": []}


@pytest.mark.asyncio
async def test_new_releases_cache_hit_skips_audible():
    """A cache hit returns immediately without scanning Audible."""
    session = AsyncMock()
    cached = [{"asin": "B001", "title": "Cached"}]
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=cached)), \
         patch.object(releases, "audible_get", new=AsyncMock()) as mock_get:
        result = await releases.get_new_releases("us", session, days=30)
        assert result == cached
        mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_coming_soon_cache_hit_skips_audible():
    """A cache hit returns immediately without scanning Audible."""
    session = AsyncMock()
    cached = [{"asin": "B002", "title": "Cached"}]
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=cached)), \
         patch.object(releases, "audible_get", new=AsyncMock()) as mock_get:
        result = await releases.get_coming_soon("us", session, days=30)
        assert result == cached
        mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_new_releases_windows_recent_past():
    """New releases collects books within the look-back window and stops below it."""
    # Descending by release date: future (skip), in-window, then below window (stop)
    page = _page(
        _product("BFUTURE", 10),   # future — skipped
        _product("BRECENT1", -5),  # in window
        _product("BRECENT2", -20), # in window
        _product("BOLD", -40),     # below 30-day window — triggers stop
    )
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=AsyncMock(side_effect=[page, _empty()])):
        result = await releases.get_new_releases("us", AsyncMock(), days=30)
        asins = [b["asin"] for b in result]
        assert asins == ["BRECENT1", "BRECENT2"]


@pytest.mark.asyncio
async def test_coming_soon_windows_future_and_sorts_soonest_first():
    """Coming soon collects upcoming books, stops at released, sorts soonest first."""
    page = _page(
        _product("BFAR", 100),     # beyond 30-day window — skipped
        _product("BLATER", 20),    # in window
        _product("BSOONER", 5),    # in window
        _product("BRELEASED", -1), # already released — triggers stop
    )
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=AsyncMock(side_effect=[page, _empty()])):
        result = await releases.get_coming_soon("us", AsyncMock(), days=30)
        asins = [b["asin"] for b in result]
        # soonest first
        assert asins == ["BSOONER", "BLATER"]


@pytest.mark.asyncio
async def test_new_releases_caches_with_midnight_ttl():
    """On a miss, results get cached with a TTL to the next UTC midnight."""
    page = _page(_product("BRECENT", -2), _product("BOLD", -40))
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()) as mock_set, \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=AsyncMock(side_effect=[page, _empty()])):
        await releases.get_new_releases("us", AsyncMock(), days=30)
        assert mock_set.await_count == 1
        _, kwargs = mock_set.call_args
        assert "ttl_seconds" in kwargs
        assert 0 < kwargs["ttl_seconds"] <= 86400


@pytest.mark.asyncio
async def test_empty_scan_returns_empty_and_does_not_cache():
    """If the scan yields nothing in-window, return empty and don't cache."""
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()) as mock_set, \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=AsyncMock(return_value=_empty())):
        result = await releases.get_new_releases("us", AsyncMock(), days=30)
        assert result == []
        mock_set.assert_not_called()