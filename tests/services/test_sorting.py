"""
sort_dicts tests — sorting live (Audible-backed) response lists.
"""

# Local
from app.services.sorting import sort_dicts, BOOK_SORT_FIELDS

# A small book list with a None rating to prove null handling.
BOOKS = [
    {"asin": "B001", "title": "Beta", "rating": 3.0},
    {"asin": "B002", "title": "Alpha", "rating": None},
    {"asin": "B003", "title": "Gamma", "rating": 4.5},
]


# ============================================================
# SORT BEHAVIOR
# ============================================================

def test_sort_dicts_rating_desc_nulls_last():
    """Descending rating puts highest first and None at the end."""
    result = sort_dicts(BOOKS, "rating", "desc", BOOK_SORT_FIELDS)
    assert [b["asin"] for b in result] == ["B003", "B001", "B002"]


def test_sort_dicts_rating_asc_nulls_last():
    """Ascending rating puts lowest first; None still sorts last."""
    result = sort_dicts(BOOKS, "rating", "asc", BOOK_SORT_FIELDS)
    assert [b["asin"] for b in result] == ["B001", "B003", "B002"]


def test_sort_dicts_title_asc():
    """String fields sort alphabetically."""
    result = sort_dicts(BOOKS, "title", "asc", BOOK_SORT_FIELDS)
    assert [b["title"] for b in result] == ["Alpha", "Beta", "Gamma"]


def test_sort_dicts_none_sort_preserves_order():
    """No sort field returns the list in its original order."""
    result = sort_dicts(BOOKS, None, "asc", BOOK_SORT_FIELDS)
    assert [b["asin"] for b in result] == ["B001", "B002", "B003"]


def test_sort_dicts_unknown_field_preserves_order():
    """A field not in the allow-list leaves the list unchanged."""
    result = sort_dicts(BOOKS, "notAField", "asc", BOOK_SORT_FIELDS)
    assert [b["asin"] for b in result] == ["B001", "B002", "B003"]


def test_sort_dicts_empty_list():
    """An empty list sorts to an empty list."""
    assert sort_dicts([], "rating", "asc", BOOK_SORT_FIELDS) == []


def test_sort_dicts_missing_key_sorts_last():
    """Items missing the sort key entirely sort to the end."""
    items = [
        {"asin": "B001", "rating": 2.0},
        {"asin": "B002"},  # no rating key at all
        {"asin": "B003", "rating": 5.0},
    ]
    result = sort_dicts(items, "rating", "desc", BOOK_SORT_FIELDS)
    assert [b["asin"] for b in result] == ["B003", "B001", "B002"]


def test_sort_dicts_does_not_mutate_input():
    """Sorting returns a new ordering without reordering the caller's list."""
    original = list(BOOKS)
    sort_dicts(BOOKS, "rating", "desc", BOOK_SORT_FIELDS)
    assert BOOKS == original