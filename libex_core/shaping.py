"""
Filtering and sorting for book lists that are already assembled as response
dicts, the shape the live (Audible-backed) endpoints work in.

Everything here is plain Python over dicts: no database, no web framework. The
field specs below are the single definition of what can be filtered and sorted
on a live list, so a front end (an HTTP layer, a CLI) can build its own
parameter surface from them and never drift from what the functions apply.

Only filters that are cheap and meaningful on an in-memory, already-scoped list
are supported: numeric ranges, categorical/boolean equality, plan membership,
and genre name matching. Heavy free-text search (title, description, etc.) is
left to a database-backed endpoint, which has indexes for it.
"""

# Standard library
from enum import Enum
from typing import Any, NamedTuple


class SortOrder(str, Enum):
    asc = "asc"
    desc = "desc"


# Sortable book fields, as clients see them (camelCase response keys).
# Text-heavy fields (description, summary) are intentionally excluded.
BOOK_SORT_FIELDS: tuple[str, ...] = (
    "title",
    "releaseDate",
    "rating",
    "lengthMinutes",
    "language",
    "publisher",
    "updatedAt",
)

# Built from the field list so the enum members are exactly the sortable
# field names, with no drift between what is allowed and what is offered.
BookSortField = Enum(
    "BookSortField",
    {field: field for field in BOOK_SORT_FIELDS},
    type=str,
)


class FilterSpec(NamedTuple):
    """One live book filter: its parameter name, value type and description."""

    name: str
    type: type
    description: str


# The live filter surface, in the order it is presented to callers.
BOOK_FILTER_SPECS: tuple[FilterSpec, ...] = (
    FilterSpec("language", str, "Filter by language (exact match)"),
    FilterSpec("book_format", str, "Filter by book format (e.g. unabridged)"),
    FilterSpec("explicit", bool, "Filter by explicit"),
    FilterSpec("whisper_sync", bool, "Filter by Whispersync availability"),
    FilterSpec("has_pdf", bool, "Filter by PDF companion availability"),
    FilterSpec("is_vvab", bool, "Filter by VVAB (virtual voice audiobook) status"),
    FilterSpec("plan_name", str, "Filter by Audible plan name (e.g. US Minerva)"),
    FilterSpec("rating_better_than", float, "Minimum rating"),
    FilterSpec("rating_worse_than", float, "Maximum rating"),
    FilterSpec("longer_than", int, "Minimum length in minutes"),
    FilterSpec("shorter_than", int, "Maximum length in minutes"),
    FilterSpec("genre", str, "Filter by genre or tag name (partial match, e.g. 'fantasy')"),
)

# Which keys filter_dicts understands. Kept as a set so callers (and tests)
# have one place to see what's filterable.
BOOK_FILTER_FIELDS: set[str] = {spec.name for spec in BOOK_FILTER_SPECS}

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


def sort_dicts(
    items: list[dict[str, Any]],
    sort: str | None,
    order: str | None,
    allowed: Any,
) -> list[dict[str, Any]]:
    """
    Sorts an already-built list of response dicts (live Audible endpoints).

    - sort: API field name; must be in `allowed`. If None or unknown, the
      list is returned unchanged (preserving the order Audible returned).
    - order: "asc" or "desc" (defaults to "asc").
    - allowed: the resource's allow-list (any container of field names, such
      as a tuple or the keys of a dict); only membership is tested.

    Items missing the field, or with a None value, sort to the end regardless
    of direction, since None can't be compared to real values. This matches
    the nulls-last ordering of the database sorter.
    """
    if not sort or sort not in allowed:
        return items

    reverse = (order or "asc").lower() == "desc"

    # Sort the present ones, then append the missing ones.
    present = [i for i in items if i.get(sort) is not None]
    missing = [i for i in items if i.get(sort) is None]
    present.sort(key=lambda i: i.get(sort), reverse=reverse)
    return present + missing
