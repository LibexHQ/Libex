"""
filter_dicts tests — filtering live (Audible-backed) response lists.
"""

# Local
from app.services.filtering import filter_dicts

# A small book list covering the filterable fields, with a None rating/length
# entry to prove numeric filters exclude missing values.
BOOKS = [
    {
        "asin": "B1", "rating": 4.5, "lengthMinutes": 1300, "language": "english",
        "bookFormat": "unabridged", "explicit": False, "whisperSync": True,
        "hasPdf": False, "isVvab": False, "plans": ["US Minerva", "AccessViaMusic"],
        "genres": [{"name": "Science Fiction & Fantasy", "type": "Genres"}, {"name": "Epic", "type": "Tags"}],
    },
    {
        "asin": "B2", "rating": 2.0, "lengthMinutes": 200, "language": "german",
        "bookFormat": "abridged", "explicit": True, "whisperSync": False,
        "hasPdf": True, "isVvab": True, "plans": ["AccessViaMusic"],
        "genres": [{"name": "Mystery", "type": "Genres"}],
    },
    {
        "asin": "B3", "rating": None, "lengthMinutes": None, "language": "english",
        "bookFormat": "unabridged", "explicit": False, "whisperSync": True,
        "hasPdf": False, "isVvab": False, "plans": [], "genres": [],
    },
]


def _asins(result):
    return [b["asin"] for b in result]


# ============================================================
# NUMERIC RANGES
# ============================================================

def test_filter_rating_better_than():
    """Minimum rating keeps only books at or above it."""
    assert _asins(filter_dicts(BOOKS, {"rating_better_than": 4.0})) == ["B1"]


def test_filter_rating_worse_than():
    """Maximum rating keeps only books at or below it."""
    assert _asins(filter_dicts(BOOKS, {"rating_worse_than": 3.0})) == ["B2"]


def test_filter_longer_than():
    """Minimum length keeps only longer books."""
    assert _asins(filter_dicts(BOOKS, {"longer_than": 600})) == ["B1"]


def test_filter_shorter_than():
    """Maximum length keeps only shorter books."""
    assert _asins(filter_dicts(BOOKS, {"shorter_than": 500})) == ["B2"]


def test_filter_numeric_excludes_missing_value():
    """A book with no rating is excluded by a rating filter (like SQL NULL)."""
    result = filter_dicts(BOOKS, {"rating_better_than": 0.0})
    assert "B3" not in _asins(result)


# ============================================================
# CATEGORICAL / BOOLEAN
# ============================================================

def test_filter_language():
    """Language filter is exact match."""
    assert _asins(filter_dicts(BOOKS, {"language": "english"})) == ["B1", "B3"]


def test_filter_book_format():
    """Book format filter is exact match."""
    assert _asins(filter_dicts(BOOKS, {"book_format": "abridged"})) == ["B2"]


def test_filter_explicit():
    """Boolean filter matches the flag."""
    assert _asins(filter_dicts(BOOKS, {"explicit": True})) == ["B2"]


def test_filter_is_vvab_false():
    """Boolean False is honored, not treated as 'no filter'."""
    assert _asins(filter_dicts(BOOKS, {"is_vvab": False})) == ["B1", "B3"]


def test_filter_plan_membership():
    """Plan filter matches books whose plans list contains the plan."""
    assert _asins(filter_dicts(BOOKS, {"plan_name": "US Minerva"})) == ["B1"]


# ============================================================
# GENRE
# ============================================================

def test_filter_genre_partial_match():
    """Genre matches both Genres and Tags by partial name."""
    assert _asins(filter_dicts(BOOKS, {"genre": "fantasy"})) == ["B1"]


def test_filter_genre_no_match():
    """A genre nothing has returns nothing."""
    assert filter_dicts(BOOKS, {"genre": "horror"}) == []


# ============================================================
# COMPOSITION & PASS-THROUGH
# ============================================================

def test_filter_multiple_and_together():
    """Multiple filters combine with AND."""
    result = filter_dicts(BOOKS, {"language": "english", "rating_better_than": 4.0})
    assert _asins(result) == ["B1"]


def test_filter_no_filters_returns_unchanged():
    """An empty/all-None filter set returns the list as-is."""
    assert filter_dicts(BOOKS, {}) == BOOKS
    assert filter_dicts(BOOKS, {"genre": None, "rating_better_than": None}) == BOOKS


def test_filter_unknown_key_ignored():
    """Keys outside the supported set have no effect."""
    assert filter_dicts(BOOKS, {"title": "whatever"}) == BOOKS


def test_filter_empty_list():
    """An empty list filters to an empty list."""
    assert filter_dicts([], {"rating_better_than": 4.0}) == []