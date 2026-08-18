"""
Cache manager unit tests.
Tests cache key builders and cache operations with mocked database.
"""

# Standard library

# Third party

# Local
from app.services.cache.manager import (
    book_key,
    author_key,
    series_key,
    stats_key,
    chapters_key,
    author_books_key,
    series_books_key,
    _safe_key_for_log,
    _REDACTED_KEY,
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


def test_stats_key_format():
    """Stats cache key has correct format."""
    assert stats_key() == "db_stats"


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


# ============================================================
# LOG-SAFETY TESTS
#
# Every builder above composes only region, ASIN and enum-bounded
# arguments, so it cannot itself carry caller text into a key. The
# tests below instead pin get/set/invalidate's own backstop --
# _safe_key_for_log -- which every logged cacheKey passes through: a
# key built from something a caller typed must never survive into the
# log line it is attached to, whatever put it there.
# ============================================================

def test_safe_key_for_log_returns_an_ordinary_key_unchanged():
    """A key built the normal way, from region/ASIN builders, logs verbatim."""
    key = book_key("B08G9PRS1K", "us")
    assert _safe_key_for_log(key) == key


def test_safe_key_for_log_redacts_caller_typed_text():
    """A key carrying free text a caller typed is replaced with the sentinel."""
    assert _safe_key_for_log("search:us:frank herbert; DROP TABLE cache") == _REDACTED_KEY


def test_safe_key_for_log_redacts_a_smuggled_second_log_line():
    """A key carrying CRLF, which would forge a second log line, never reaches the sentinel's alternative."""
    smuggled = "book:us:B08G9PRS1K\r\ncacheKey=forged"
    result = _safe_key_for_log(smuggled)
    assert result == _REDACTED_KEY
    assert "\r" not in result and "\n" not in result


def test_safe_key_for_log_redacts_an_overlong_key():
    """A key longer than the safe-value bound is redacted rather than truncated and logged."""
    assert _safe_key_for_log("book:us:" + "a" * 100) == _REDACTED_KEY