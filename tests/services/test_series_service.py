"""
Series service unit tests.
Tests normalization helpers without hitting Audible.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
from app.services.audible.series import _normalize_series
from app.core.utils import strip_html


# ============================================================
# CLEAN DESCRIPTION TESTS
# ============================================================

def test_clean_description_strips_html():
    """HTML tags are stripped from description."""
    result = strip_html("<p>A great series.</p>")
    assert result == "A great series."


def test_clean_description_strips_nested_html():
    """Nested HTML tags are stripped."""
    result = strip_html("<p><strong>Bold</strong> text.</p>")
    assert result == "Bold text."


def test_clean_description_returns_none_for_empty():
    """Empty string returns None."""
    assert strip_html("") is None


def test_clean_description_returns_none_for_none():
    """None input returns None."""
    assert strip_html(None) is None


def test_clean_description_strips_whitespace():
    """Leading and trailing whitespace is stripped."""
    result = strip_html("  A great series.  ")
    assert result == "A great series."


def test_clean_description_returns_none_for_whitespace_only():
    """Whitespace-only string returns None."""
    assert strip_html("   ") is None


# ============================================================
# NORMALIZE SERIES TESTS
# ============================================================

def test_normalize_series_extracts_asin():
    """Normalized series includes ASIN."""
    product = {"asin": "B000SERIES1", "title": "Dune", "publisher_summary": "A great series."}
    result = _normalize_series(product, "us")
    assert result["asin"] == "B000SERIES1"


def test_normalize_series_extracts_name():
    """Normalized series includes name field matching AudiMeta SeriesDto."""
    product = {"asin": "B000SERIES1", "title": "Dune Chronicles", "publisher_summary": None}
    result = _normalize_series(product, "us")
    assert result["name"] == "Dune Chronicles"


def test_normalize_series_sets_region():
    """Normalized series includes provided region."""
    product = {"asin": "B000SERIES1", "title": "Dune", "publisher_summary": None}
    result = _normalize_series(product, "uk")
    assert result["region"] == "uk"


def test_normalize_series_cleans_description():
    """Normalized series description has HTML stripped."""
    product = {"asin": "B000SERIES1", "title": "Dune", "publisher_summary": "<p>A great series.</p>"}
    result = _normalize_series(product, "us")
    assert result["description"] == "A great series."


def test_normalize_series_description_none_when_missing():
    """Normalized series description is None when not provided."""
    product = {"asin": "B000SERIES1", "title": "Dune", "publisher_summary": None}
    result = _normalize_series(product, "us")
    assert result["description"] is None


def test_normalize_series_returns_required_fields():
    """Normalized series contains all required fields matching AudiMeta SeriesDto."""
    product = {"asin": "B000SERIES1", "title": "Dune", "publisher_summary": None}
    result = _normalize_series(product, "us")
    for field in ["asin", "name", "description", "region"]:
        assert field in result, f"Missing field: {field}"


# ============================================================
# DB FALLBACK TESTS
# ============================================================

@pytest.mark.asyncio
async def test_get_series_falls_back_to_db_when_audible_fails():
    """Falls back to DB when Audible is unavailable."""
    from app.services.audible.series import get_series

    mock_session = AsyncMock()
    db_series = {
        "asin": "B000SERIES1", "name": "Dune Chronicles",
        "description": "From DB", "region": "us",
        "position": None, "updatedAt": "2024-01-01T00:00:00+00:00",
    }

    with patch("app.services.audible.series.audible_get", side_effect=Exception("Audible down")), \
         patch("app.services.audible.series.get_series_from_db", new_callable=AsyncMock, return_value=db_series), \
         patch("app.services.audible.series.cache.get", return_value=None):
        result = await get_series("B000SERIES1", "us", mock_session)
        assert result["name"] == "Dune Chronicles"
        assert result["description"] == "From DB"


@pytest.mark.asyncio
async def test_get_series_falls_back_to_cache_when_db_empty():
    """Falls back to cache when Audible is down and DB has no results."""
    from app.services.audible.series import get_series

    mock_session = AsyncMock()
    cached_series = {
        "asin": "B000SERIES1", "name": "Dune Chronicles (cached)",
        "description": None, "region": "us",
        "position": None, "updatedAt": None,
    }

    with patch("app.services.audible.series.audible_get", side_effect=Exception("Audible down")), \
         patch("app.services.audible.series.get_series_from_db", new_callable=AsyncMock, return_value=None), \
         patch("app.services.audible.series.cache.get", return_value=cached_series):
        result = await get_series("B000SERIES1", "us", mock_session)
        assert result["name"] == "Dune Chronicles (cached)"


@pytest.mark.asyncio
async def test_get_series_writes_to_db_on_success():
    """Writes series profile to DB after successful Audible fetch."""
    from app.services.audible.series import get_series

    mock_session = AsyncMock()
    mock_response = {
        "response_groups": ["product_attrs", "product_desc"],
        "product": {
            "asin": "B000SERIES1",
            "title": "Dune Chronicles",
            "publisher_summary": "A great series.",
        }
    }

    with patch("app.services.audible.series.audible_get", return_value=mock_response), \
         patch("app.services.audible.series.upsert_series_profile", new_callable=AsyncMock) as mock_upsert, \
         patch("app.services.audible.series.cache.get", return_value=None), \
         patch("app.services.audible.series.cache.set", new_callable=AsyncMock):
        await get_series("B000SERIES1", "us", mock_session)
        mock_upsert.assert_called_once()


@pytest.mark.asyncio
async def test_search_series_includes_db_results():
    """Series search augments Audible results with DB matches."""
    from app.services.audible.series import search_series

    mock_session = AsyncMock()
    db_series = {
        "asin": "B000SERIES2", "name": "Dune Expanded",
        "description": "From DB", "region": "us", "position": None,
        "updatedAt": "2024-01-01T00:00:00+00:00",
    }

    audible_product_response = {
        "products": [
            {
                "asin": "B08G9PRS1K",
                "relationships": [
                    {"relationship_type": "series", "asin": "B000SERIES1"}
                ]
            }
        ]
    }
    series_detail_response = {
        "response_groups": ["product_attrs", "product_desc"],
        "product": {"asin": "B000SERIES1", "title": "Dune Chronicles", "publisher_summary": None}
    }

    with patch("app.services.audible.series.audible_get", side_effect=[audible_product_response, series_detail_response]), \
         patch("app.services.audible.series.search_series_from_db", new_callable=AsyncMock, return_value=[db_series]), \
         patch("app.services.audible.series.upsert_series_profile", new_callable=AsyncMock), \
         patch("app.services.audible.series.cache.get", return_value=None), \
         patch("app.services.audible.series.cache.set", new_callable=AsyncMock):
        results = await search_series("Dune", "us", mock_session)
        asins = [r["asin"] for r in results]
        assert "B000SERIES1" in asins
        assert "B000SERIES2" in asins


@pytest.mark.asyncio
async def test_search_series_deduplicates_audible_and_db_results():
    """Search does not return same series from both Audible and DB."""
    from app.services.audible.series import search_series

    mock_session = AsyncMock()
    same_series = {
        "asin": "B000SERIES1", "name": "Dune Chronicles",
        "description": None, "region": "us", "position": None, "updatedAt": None,
    }

    audible_product_response = {
        "products": [
            {
                "asin": "B08G9PRS1K",
                "relationships": [
                    {"relationship_type": "series", "asin": "B000SERIES1"}
                ]
            }
        ]
    }
    series_detail_response = {
        "response_groups": ["product_attrs", "product_desc"],
        "product": {"asin": "B000SERIES1", "title": "Dune Chronicles", "publisher_summary": None}
    }

    with patch("app.services.audible.series.audible_get", side_effect=[audible_product_response, series_detail_response]), \
         patch("app.services.audible.series.search_series_from_db", new_callable=AsyncMock, return_value=[same_series]), \
         patch("app.services.audible.series.upsert_series_profile", new_callable=AsyncMock), \
         patch("app.services.audible.series.cache.get", return_value=None), \
         patch("app.services.audible.series.cache.set", new_callable=AsyncMock):
        results = await search_series("Dune", "us", mock_session)
        asins = [r["asin"] for r in results]
        assert asins.count("B000SERIES1") == 1