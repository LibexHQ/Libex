"""
Shared filtering helper for live (Audible-backed) list endpoints.

The DB layer filters in Postgres via WHERE clauses (app/services/db/filtering.py).
Live endpoints get their books back already assembled as response dicts, so they
filter the list in Python instead — the parallel of sort_dicts in sorting.py.

Only filters that are cheap and meaningful on an in-memory, already-scoped list
are supported: numeric ranges, categorical/boolean equality, plan membership,
and genre name matching. Heavy free-text search (title, description, etc.) is
intentionally left to the DB endpoint, which has indexes for it — scanning long
text blobs in Python per request would be wasteful and duplicate /db/book.
"""

# Standard library
from typing import Any

# The live filter surface: which keys filter_dicts understands. Kept as a set
# so callers (and tests) have one place to see what's filterable.
BOOK_FILTER_FIELDS: set[str] = {
    "language",
    "book_format",
    "explicit",
    "whisper_sync",
    "has_pdf",
    "is_vvab",
    "plan_name",
    "rating_better_than",
    "rating_worse_than",
    "longer_than",
    "shorter_than",
    "genre",
}

# Map the bool/equality filter names to the camelCase dict keys they test.
_EQUALITY_KEYS = {
    "language": "language",
    "book_format": "bookFormat",
    "explicit": "explicit",
    "whisper_sync": "whisperSync",
    "has_pdf": "hasPdf",
    "is_vvab": "isVvab",
}


def _matches(book: dict[str, Any], filters: dict[str, Any]) -> bool:
    """True if a single book dict passes every active filter."""
    for name, key in _EQUALITY_KEYS.items():
        wanted = filters.get(name)
        if wanted is not None and book.get(key) != wanted:
            return False

    plan_name = filters.get("plan_name")
    if plan_name is not None and plan_name not in (book.get("plans") or []):
        return False

    rating = book.get("rating")
    if filters.get("rating_better_than") is not None:
        if rating is None or rating < filters["rating_better_than"]:
            return False
    if filters.get("rating_worse_than") is not None:
        if rating is None or rating > filters["rating_worse_than"]:
            return False

    length = book.get("lengthMinutes")
    if filters.get("longer_than") is not None:
        if length is None or length < filters["longer_than"]:
            return False
    if filters.get("shorter_than") is not None:
        if length is None or length > filters["shorter_than"]:
            return False

    genre = filters.get("genre")
    if genre is not None:
        needle = genre.lower()
        names = [g.get("name", "") for g in (book.get("genres") or [])]
        if not any(needle in n.lower() for n in names):
            return False

    return True


def filter_dicts(
    items: list[dict[str, Any]],
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Filters an already-built list of book response dicts (live endpoints).

    - filters: a dict of {filter_name: value}; None values are ignored. Only
      the keys in BOOK_FILTER_FIELDS have any effect; unknown keys are skipped.
    - Returns a new list of the books that pass every active filter, preserving
      the input order. If no filters are active, the input is returned unchanged.

    Books missing the field a numeric/range filter targets are excluded by that
    filter (you can't be "longer than 600" with no length), mirroring how a SQL
    comparison drops NULLs.
    """
    active = {k: v for k, v in filters.items() if k in BOOK_FILTER_FIELDS and v is not None}
    if not active:
        return items
    return [book for book in items if _matches(book, active)]