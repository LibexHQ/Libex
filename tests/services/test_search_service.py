"""
Audible search service unit tests.

search() and quick_search() must raise AudibleAPIException when Audible is
unreachable -- an empty list here would be indistinguishable from a genuine
zero-result search, which is exactly the ambiguity a caller must not be
handed. A genuine NotFoundException (Audible answered and said no, or in
quick_search's fallback ladder, nothing at all was ever found) still comes
back as an empty list.
"""

# Standard library
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest

# Local
from app.core.exceptions import AudibleAPIException, NotFoundException
from app.services.audible.search import quick_search, search


# ============================================================
# SEARCH() — TOTAL FAILURE
# ============================================================

@pytest.mark.asyncio
async def test_search_raises_audible_api_exception_on_failure():
    """search() has no DB/cache fallback of its own -- any failure other
    than a genuine NotFoundException must raise, not return []."""
    with patch("app.services.audible.search.audible_get", new=AsyncMock(side_effect=RuntimeError("Audible down"))):
        with pytest.raises(AudibleAPIException) as exc:
            await search(region="us", session=MagicMock(), title="Dune")

    assert exc.value.message == "Audible search failed"
    assert exc.value.upstream_status is None


@pytest.mark.asyncio
async def test_search_returns_empty_list_on_a_genuine_not_found():
    with patch("app.services.audible.search.audible_get", new=AsyncMock(side_effect=NotFoundException("gone"))):
        result = await search(region="us", session=MagicMock(), title="Dune")

    assert result == []


@pytest.mark.asyncio
async def test_search_carries_upstream_status_when_audible_get_reported_one():
    upstream_exc = AudibleAPIException("Audible API returned 503 for https://x", upstream_status=503)
    with patch("app.services.audible.search.audible_get", new=AsyncMock(side_effect=upstream_exc)):
        with pytest.raises(AudibleAPIException) as exc:
            await search(region="us", session=MagicMock(), title="Dune")

    assert exc.value.upstream_status == 503


# ============================================================
# QUICK_SEARCH() — SUGGESTIONS FAILURE
# ============================================================

@pytest.mark.asyncio
async def test_quick_search_raises_audible_api_exception_when_suggestions_call_fails():
    with patch("app.services.audible.search.audible_get", new=AsyncMock(side_effect=RuntimeError("Audible down"))):
        with pytest.raises(AudibleAPIException) as exc:
            await quick_search("dune", "us", MagicMock())

    assert exc.value.message == "Audible quick search failed"
    assert exc.value.upstream_status is None


@pytest.mark.asyncio
async def test_quick_search_returns_empty_list_on_a_genuine_not_found():
    with patch("app.services.audible.search.audible_get", new=AsyncMock(side_effect=NotFoundException("gone"))):
        result = await quick_search("dune", "us", MagicMock())

    assert result == []


@pytest.mark.asyncio
async def test_quick_search_raises_when_hydrating_the_suggested_asins_fails():
    """Suggestions succeed and return ASINs, but hydrating them
    (get_books_by_asins) fails with nothing to fall back to -- that failure
    is not caught locally in quick_search, so it must reach the caller as
    AudibleAPIException via the outer except, not surface as []."""
    suggestions_response = {
        "model": {
            "items": [
                {"view": {"template": "AsinRow"}, "model": {"product_metadata": {"asin": "B08G9PRS1K"}}},
            ]
        }
    }
    inner = AudibleAPIException("Audible unavailable and no cached data found")

    with patch("app.services.audible.search.audible_get", new=AsyncMock(return_value=suggestions_response)), \
         patch("app.services.audible.search.get_books_by_asins", new=AsyncMock(side_effect=inner)):
        with pytest.raises(AudibleAPIException) as exc:
            await quick_search("dune", "us", MagicMock())

    assert exc.value.message == "Audible quick search failed"


@pytest.mark.asyncio
async def test_quick_search_compound_fallback_still_reaches_db_when_catalog_search_fails():
    """The compound ABS-style query fallback ("Author - Title") catches an
    AudibleAPIException from its own search() call locally -- a transport
    failure on that one avenue must not stop the local DB from still being
    tried, exactly as it is on a genuine zero-result catalog search."""
    suggestions_response = {"model": {"items": []}}
    db_result = [{"asin": "B0DBRESULT1", "title": "DB Fallback Book"}]

    with patch("app.services.audible.search.audible_get", new=AsyncMock(return_value=suggestions_response)), \
         patch("app.services.audible.search.search", new=AsyncMock(side_effect=AudibleAPIException("Audible search failed"))), \
         patch("app.services.audible.search.search_books_from_db", new=AsyncMock(return_value=db_result)) as mock_db:
        result = await quick_search("Frank Herbert - Dune", "us", MagicMock())

    mock_db.assert_awaited_once()
    assert result == db_result


@pytest.mark.asyncio
async def test_quick_search_compound_fallback_returns_empty_list_when_db_also_has_nothing():
    """Same failed-catalog-search path, but the DB fallback also comes back
    empty -- this is a genuine "nothing found," not an outage, so it returns
    [] rather than raising."""
    suggestions_response = {"model": {"items": []}}

    with patch("app.services.audible.search.audible_get", new=AsyncMock(return_value=suggestions_response)), \
         patch("app.services.audible.search.search", new=AsyncMock(side_effect=AudibleAPIException("Audible search failed"))), \
         patch("app.services.audible.search.search_books_from_db", new=AsyncMock(return_value=[])):
        result = await quick_search("Frank Herbert - Dune", "us", MagicMock())

    assert result == []
