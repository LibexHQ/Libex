"""
Cache manager unit tests.
Tests cache key builders and cache operations with mocked database.
"""

# Standard library
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest

# Local
from app.services.cache.manager import (
    book_key,
    author_key,
    series_key,
    search_key,
    chapters_key,
    author_books_key,
    series_books_key,
)


# ============================================================
# KEY BUILDER TESTS
# ============================================================

def test_book_key_format():
    """Book cache key has correct format."""
    assert book_key("B08G9PRS1K", "us") == "book:us:B08G9PRS1K"


def test_book_key_includes_region():
    """Book cache key includes region."""
    assert book_key("B08G9PRS1K", "uk") == "book:uk:B08G9PRS1K"


def test_author_key_format():
    """Author cache key has correct format."""
    assert author_key("B000TEST01", "us") == "author:us:B000TEST01"


def test_series_key_format():
    """Series cache key has correct format."""
    assert series_key("B000SERIES", "us") == "series:us:B000SERIES"


def test_chapters_key_format():
    """Chapters cache key has correct format."""
    assert chapters_key("B08G9PRS1K", "us") == "chapters:us:B08G9PRS1K"


def test_author_books_key_format():
    """Author books cache key has correct format."""
    assert author_books_key("B000TEST01", "us") == "author_books:us:B000TEST01"


def test_series_books_key_format():
    """Series books cache key has correct format."""
    assert series_books_key("B000SERIES", "us") == "series_books:us:B000SERIES"


def test_search_key_normalizes_case():
    """Search cache key lowercases the query."""
    assert search_key("Dune", "us") == "search:us:dune"


def test_search_key_normalizes_spaces():
    """Search cache key replaces spaces with plus signs."""
    assert search_key("frank herbert", "us") == "search:us:frank+herbert"


def test_different_regions_produce_different_keys():
    """Same ASIN in different regions produces different cache keys."""
    us_key = book_key("B08G9PRS1K", "us")
    uk_key = book_key("B08G9PRS1K", "uk")
    assert us_key != uk_key


def test_different_asins_produce_different_keys():
    """Different ASINs produce different cache keys."""
    key1 = book_key("B08G9PRS1K", "us")
    key2 = book_key("B000000001", "us")
    assert key1 != key2