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
         patch("app.services.audible.series.persist_series_background") as mock_persist, \
         patch("app.services.audible.series.cache.get", return_value=None):
        await get_series("B000SERIES1", "us", mock_session)
        mock_persist.assert_called_once()


# ============================================================
# TOTAL FAILURE -- AUDIBLE DOWN, NOTHING BACKSTOPS IT
# ============================================================
# When Audible is unreachable and neither the DB nor the cache has anything
# to answer with, that is silence -- Libex could not find out -- not
# Audible confirming an absence. Each of these must raise
# AudibleAPIException, not NotFoundException and not an empty result.

@pytest.mark.asyncio
async def test_get_series_raises_audible_api_exception_when_nothing_backstops_it():
    from app.services.audible.series import get_series
    from app.core.exceptions import AudibleAPIException

    mock_session = AsyncMock()

    with patch("app.services.audible.series.audible_get", side_effect=RuntimeError("Audible down")), \
         patch("app.services.audible.series.get_series_from_db", new_callable=AsyncMock, return_value=None), \
         patch("app.services.audible.series.cache.get", new=AsyncMock(return_value=None)):
        with pytest.raises(AudibleAPIException) as exc:
            await get_series("B000SERIES1", "us", mock_session)

    assert exc.value.message == "Audible unavailable and no cached series data found"
    assert exc.value.upstream_status is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised,expected_upstream_status",
    [
        pytest.param("audible_api", 503, id="audible_api_exception_carries_its_status"),
        pytest.param("plain", None, id="plain_exception_has_no_status"),
    ],
)
async def test_get_series_logs_warning_before_raising(raised, expected_upstream_status):
    """The no-backstop outage path must log a WARNING carrying every
    diagnostic field before raising, and upstream_status must reflect the
    raised exception's own value when it is an AudibleAPIException, or None
    for any other exception type."""
    from app.services.audible.series import get_series
    from app.core.exceptions import AudibleAPIException

    exc = (
        AudibleAPIException("upstream 503", upstream_status=503)
        if raised == "audible_api"
        else RuntimeError("Audible down")
    )
    mock_session = AsyncMock()

    with patch("app.services.audible.series.audible_get", side_effect=exc), \
         patch("app.services.audible.series.get_series_from_db", new_callable=AsyncMock, return_value=None), \
         patch("app.services.audible.series.cache.get", new=AsyncMock(return_value=None)), \
         patch("app.services.audible.series.logger") as mock_logger:
        with pytest.raises(AudibleAPIException):
            await get_series("B000SERIES1", "us", mock_session)

    mock_logger.warning.assert_called_once_with(
        "Audible unavailable and no cached series data found",
        extra={
            "series_asin": "B000SERIES1",
            "region": "us",
            "error": str(exc),
            "upstream_status": expected_upstream_status,
        },
    )


@pytest.mark.asyncio
async def test_get_series_books_raises_audible_api_exception_when_nothing_backstops_it():
    from app.services.audible.series import get_series_books
    from app.core.exceptions import AudibleAPIException

    mock_session = AsyncMock()

    with patch("app.services.audible.series.audible_get", side_effect=RuntimeError("Audible down")), \
         patch("app.services.audible.series.cache.get", new=AsyncMock(return_value=None)):
        with pytest.raises(AudibleAPIException) as exc:
            await get_series_books("B000SERIES1", "us", mock_session)

    assert exc.value.message == "Audible unavailable and no cached series books found"
    assert exc.value.upstream_status is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised,expected_upstream_status",
    [
        pytest.param("audible_api", 502, id="audible_api_exception_carries_its_status"),
        pytest.param("plain", None, id="plain_exception_has_no_status"),
    ],
)
async def test_get_series_books_logs_warning_before_raising(raised, expected_upstream_status):
    """The no-backstop outage path must log a WARNING carrying every
    diagnostic field before raising, and upstream_status must reflect the
    raised exception's own value when it is an AudibleAPIException, or None
    for any other exception type."""
    from app.services.audible.series import get_series_books
    from app.core.exceptions import AudibleAPIException

    exc = (
        AudibleAPIException("upstream 502", upstream_status=502)
        if raised == "audible_api"
        else RuntimeError("Audible down")
    )
    mock_session = AsyncMock()

    with patch("app.services.audible.series.audible_get", side_effect=exc), \
         patch("app.services.audible.series.cache.get", new=AsyncMock(return_value=None)), \
         patch("app.services.audible.series.logger") as mock_logger:
        with pytest.raises(AudibleAPIException):
            await get_series_books("B000SERIES1", "us", mock_session)

    mock_logger.warning.assert_called_once_with(
        "Audible unavailable and no cached series books found",
        extra={
            "series_asin": "B000SERIES1",
            "region": "us",
            "error": str(exc),
            "upstream_status": expected_upstream_status,
        },
    )


@pytest.mark.asyncio
async def test_search_series_raises_audible_api_exception_on_total_failure():
    """The Audible products search itself failing, with nothing to fall
    back to, must raise -- not return an empty list a caller could confuse
    with a genuine zero-result search."""
    from app.services.audible.series import search_series
    from app.core.exceptions import AudibleAPIException

    mock_session = AsyncMock()

    with patch("app.services.audible.series.audible_get", side_effect=RuntimeError("Audible down")), \
         patch("app.services.audible.series.search_series_from_db", new=AsyncMock(return_value=[])):
        with pytest.raises(AudibleAPIException) as exc:
            await search_series("Dune", "us", mock_session)

    assert exc.value.message == "Series search failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised,expected_upstream_status",
    [
        pytest.param("audible_api", 500, id="audible_api_exception_carries_its_status"),
        pytest.param("plain", None, id="plain_exception_has_no_status"),
    ],
)
async def test_search_series_outer_failure_logs_warning_before_raising(raised, expected_upstream_status):
    """The products search failing outright must log a WARNING with the
    expected diagnostic fields before raising, upstream_status must reflect
    the raised exception's own value, and the caller-authored name must
    never appear in the log call."""
    from app.services.audible.series import search_series
    from app.core.exceptions import AudibleAPIException

    exc = (
        AudibleAPIException("upstream 500", upstream_status=500)
        if raised == "audible_api"
        else RuntimeError("Audible down")
    )
    name = "Dune"
    mock_session = AsyncMock()

    with patch("app.services.audible.series.audible_get", side_effect=exc), \
         patch("app.services.audible.series.search_series_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.series.logger") as mock_logger:
        with pytest.raises(AudibleAPIException):
            await search_series(name, "us", mock_session)

    mock_logger.warning.assert_called_once_with(
        "Series search failed",
        extra={
            "name_length": len(name),
            "region": "us",
            "error": str(exc),
            "upstream_status": expected_upstream_status,
        },
    )
    logged_message, logged_kwargs = mock_logger.warning.call_args
    assert name not in logged_message[0]
    assert name not in logged_kwargs["extra"].values()


# ============================================================
# SEARCH -- PER-ITEM SKIP ON AN UNREACHABLE RELATED SERIES
# ============================================================

@pytest.mark.asyncio
async def test_search_series_skips_an_unreachable_related_series_and_keeps_the_rest():
    """One series relationship being unreachable (Audible down for that one
    fetch) must not sink a search that already has other hits to show -- it
    is logged and skipped, and the remaining, reachable series still come
    back."""
    from app.services.audible.series import search_series
    from app.core.exceptions import AudibleAPIException

    mock_session = AsyncMock()
    reachable = {
        "asin": "B000SERIES2", "name": "Reachable Series",
        "description": None, "region": "us", "position": None, "updatedAt": None,
    }

    audible_product_response = {
        "products": [
            {
                "asin": "B08G9PRS1K",
                "relationships": [
                    {"relationship_type": "series", "asin": "B000SERIES1"},
                    {"relationship_type": "series", "asin": "B000SERIES2"},
                ],
            }
        ]
    }

    async def fake_get_series(asin, region, session):
        if asin == "B000SERIES1":
            raise AudibleAPIException("Audible unavailable and no cached series data found")
        return reachable

    with patch("app.services.audible.series.audible_get", return_value=audible_product_response), \
         patch("app.services.audible.series.get_series", new=AsyncMock(side_effect=fake_get_series)), \
         patch("app.services.audible.series.search_series_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.series.logger") as mock_logger:
        results = await search_series("Dune", "us", mock_session)

    assert results == [reachable]
    mock_logger.warning.assert_called_once_with(
        "Series search: could not resolve one or more related series, skipping",
        extra={
            "region": "us",
            "skipped_num": 1,
            "skipped_asins": ["B000SERIES1"],
        },
    )


@pytest.mark.asyncio
async def test_search_series_emits_no_summary_warning_when_nothing_was_skipped():
    """The summary warning is conditional on the skip list -- a search where
    every related series resolves cleanly must not log at all."""
    from app.services.audible.series import search_series

    mock_session = AsyncMock()
    reachable = {
        "asin": "B000SERIES2", "name": "Reachable Series",
        "description": None, "region": "us", "position": None, "updatedAt": None,
    }
    audible_product_response = {
        "products": [
            {
                "asin": "B08G9PRS1K",
                "relationships": [
                    {"relationship_type": "series", "asin": "B000SERIES2"},
                ],
            }
        ]
    }

    with patch("app.services.audible.series.audible_get", return_value=audible_product_response), \
         patch("app.services.audible.series.get_series", new=AsyncMock(return_value=reachable)), \
         patch("app.services.audible.series.search_series_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.series.logger") as mock_logger:
        results = await search_series("Dune", "us", mock_session)

    assert results == [reachable]
    mock_logger.warning.assert_not_called()


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
         patch("app.services.audible.series.persist_series_background"), \
         patch("app.services.audible.series.cache.get", return_value=None):
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
         patch("app.services.audible.series.persist_series_background"), \
         patch("app.services.audible.series.cache.get", return_value=None):
        results = await search_series("Dune", "us", mock_session)
        asins = [r["asin"] for r in results]
        assert asins.count("B000SERIES1") == 1