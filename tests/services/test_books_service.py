"""
Books service unit tests.
Tests normalization and helper functions without hitting Audible.
"""

# Third party
import pytest

# Local
from app.services.audible.books import (
    _best_image,
    _parse_authors,
    _parse_narrators,
    _parse_series,
    _parse_genres,
    _normalize_product,
    _filter_products,
)


# ============================================================
# IMAGE TESTS
# ============================================================

def test_best_image_returns_highest_resolution():
    """Returns URL for highest resolution image."""
    images = {"500": "http://example.com/500.jpg", "2400": "http://example.com/2400.jpg"}
    result = _best_image(images)
    assert result == "http://example.com/2400.jpg"


def test_best_image_strips_size_suffix():
    images = {"500": "http://example.com/image._SX500_.jpg"}
    result = _best_image(images)
    assert result is not None
    assert "._" not in result


def test_best_image_returns_none_for_empty():
    """Returns None for empty image dict."""
    assert _best_image({}) is None


def test_best_image_returns_none_for_none():
    """Returns None for None input."""
    assert _best_image(None) is None


# ============================================================
# AUTHOR PARSING TESTS
# ============================================================

def test_parse_authors_returns_list():
    """Returns list of author dicts."""
    product = {"authors": [{"name": "Frank Herbert", "asin": "B000APF21M"}]}
    result = _parse_authors(product, "us")
    assert isinstance(result, list)
    assert len(result) == 1


def test_parse_authors_includes_name():
    """Author dict includes name."""
    product = {"authors": [{"name": "Frank Herbert", "asin": "B000APF21M"}]}
    result = _parse_authors(product, "us")
    assert result[0]["name"] == "Frank Herbert"


def test_parse_authors_includes_region():
    """Author dict includes region."""
    product = {"authors": [{"name": "Frank Herbert", "asin": "B000APF21M"}]}
    result = _parse_authors(product, "uk")
    assert result[0]["region"] == "uk"


def test_parse_authors_strips_tabs():
    """Author name has tabs stripped."""
    product = {"authors": [{"name": "\tFrank Herbert\t", "asin": "B000APF21M"}]}
    result = _parse_authors(product, "us")
    assert result[0]["name"] == "Frank Herbert"


def test_parse_authors_rejects_long_asin():
    """Author ASIN longer than 12 chars is set to None."""
    product = {"authors": [{"name": "Author", "asin": "TOOLONGASIN123"}]}
    result = _parse_authors(product, "us")
    assert result[0]["asin"] is None


def test_parse_authors_returns_empty_for_no_authors():
    """Returns empty list when no authors."""
    assert _parse_authors({}, "us") == []


# ============================================================
# NARRATOR TESTS
# ============================================================

def test_parse_narrators_returns_names():
    """Returns list of narrator name strings."""
    product = {"narrators": [{"name": "Scott Brick"}, {"name": "Kate Reading"}]}
    result = _parse_narrators(product)
    assert result == ["Scott Brick", "Kate Reading"]


def test_parse_narrators_returns_empty_for_none():
    """Returns empty list when no narrators."""
    assert _parse_narrators({}) == []


# ============================================================
# SERIES TESTS
# ============================================================

def test_parse_series_extracts_series():
    """Extracts series from relationships."""
    product = {
        "relationships": [
            {"relationship_type": "series", "asin": "B000SERIES1", "title": "Dune", "sequence": "1"}
        ]
    }
    result = _parse_series(product, "us")
    assert len(result) == 1
    assert result[0]["asin"] == "B000SERIES1"


def test_parse_series_ignores_non_series():
    """Ignores relationships that are not series."""
    product = {
        "relationships": [
            {"relationship_type": "episode", "asin": "B000EP1", "title": "Episode 1"}
        ]
    }
    result = _parse_series(product, "us")
    assert result == []


def test_parse_series_returns_empty_for_no_relationships():
    """Returns empty list when no relationships."""
    assert _parse_series({}, "us") == []


# ============================================================
# GENRE TESTS
# ============================================================

def test_parse_genres_extracts_names():
    """Extracts genre names from category ladders."""
    product = {
        "category_ladders": [
            {"ladder": [{"name": "Science Fiction"}, {"name": "Space Opera"}]}
        ]
    }
    result = _parse_genres(product)
    assert "Science Fiction" in result


def test_parse_genres_deduplicates():
    """Does not return duplicate genre names."""
    product = {
        "category_ladders": [
            {"ladder": [{"name": "Fiction"}]},
            {"ladder": [{"name": "Fiction"}]},
        ]
    }
    result = _parse_genres(product)
    assert result.count("Fiction") == 1


# ============================================================
# FILTER TESTS
# ============================================================

def test_filter_products_removes_unreleased():
    """Removes products with unreleased placeholder date."""
    products = [
        {"title": "Real Book", "publication_datetime": "2021-01-01T00:00:00Z"},
        {"title": "Future Book", "publication_datetime": "2200-01-01T00:00:00Z"},
    ]
    result = _filter_products(products)
    assert len(result) == 1
    assert result[0]["title"] == "Real Book"


def test_filter_products_removes_untitled():
    """Removes products without a title."""
    products = [
        {"title": "Real Book", "publication_datetime": "2021-01-01T00:00:00Z"},
        {"title": None, "publication_datetime": "2021-01-01T00:00:00Z"},
    ]
    result = _filter_products(products)
    assert len(result) == 1


def test_filter_products_returns_empty_for_empty_input():
    """Returns empty list for empty input."""
    assert _filter_products([]) == []


# ============================================================
# NORMALIZE TESTS
# ============================================================

def test_normalize_product_returns_required_fields():
    """Normalized product contains all required fields."""
    product = {
        "asin": "B08G9PRS1K",
        "title": "Dune",
        "authors": [{"name": "Frank Herbert", "asin": "B000APF21M"}],
        "narrators": [{"name": "Scott Brick"}],
        "relationships": [],
        "product_images": {"500": "http://example.com/500.jpg"},
        "category_ladders": [],
        "rating": {"overall_distribution": {"average_rating": 4.8}},
    }
    result = _normalize_product(product, "us")
    required = ["asin", "title", "authors", "narrators", "series", "region"]
    for field in required:
        assert field in result, f"Missing field: {field}"


def test_normalize_product_sets_region():
    """Normalized product has correct region."""
    product = {"asin": "B08G9PRS1K", "title": "Dune", "authors": [], "narrators": [], "relationships": [], "product_images": {}, "category_ladders": [], "rating": {}}
    result = _normalize_product(product, "uk")
    assert result["region"] == "uk"