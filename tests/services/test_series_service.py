"""
Series service unit tests.
Tests normalization helpers without hitting Audible.
"""

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


def test_normalize_series_extracts_title():
    """Normalized series includes title."""
    product = {"asin": "B000SERIES1", "title": "Dune Chronicles", "publisher_summary": None}
    result = _normalize_series(product, "us")
    assert result["title"] == "Dune Chronicles"


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
    """Normalized series contains all required fields."""
    product = {"asin": "B000SERIES1", "title": "Dune", "publisher_summary": None}
    result = _normalize_series(product, "us")
    for field in ["asin", "title", "description", "region"]:
        assert field in result, f"Missing field: {field}"