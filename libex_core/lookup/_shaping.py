"""
Filtering and sorting a lookup's books, checked before anything is sent.

The hosted routes take their filters and sort from query parameters, which
FastAPI validates: an unknown sort field or a value of the wrong type is
refused before the route runs. The library has no such layer, and the
functions in libex_core.shaping skip what they do not recognise, so a typo in
a filter name would quietly return an unfiltered list. check_shaping is that
refusal, raised as ValueError and never repeating what the caller passed.
shape_books then applies the filters and the sort exactly as the routes do.
"""

# Standard library
from typing import Any

# Core
from libex_core.shaping import (
    BOOK_FILTER_FIELDS,
    BOOK_FILTER_SPECS,
    BOOK_SORT_FIELDS,
    filter_dicts,
    sort_dicts,
)

_ORDERS = ("asc", "desc")


def _fits(value: Any, expected: type) -> bool:
    """True when value is acceptable for a filter of the given type. A bool is
    not a number here, and an int stands in for a float."""
    if expected is bool:
        return isinstance(value, bool)
    if expected is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


def check_shaping(
    filters: dict[str, Any] | None, sort: str | None, order: str | None
) -> None:
    """
    Raises ValueError for a filter name that is not one of
    libex_core.shaping.BOOK_FILTER_FIELDS, a filter value of the wrong type, a
    sort that is not one of BOOK_SORT_FIELDS, or an order that is not asc or
    desc. A filter whose value is None is ignored, as on the routes.
    """
    if filters is not None:
        if not isinstance(filters, dict):
            raise ValueError("filters must be a dict")
        unknown = [name for name in filters if name not in BOOK_FILTER_FIELDS]
        if unknown:
            raise ValueError(
                "unknown filter; allowed: " + ", ".join(sorted(BOOK_FILTER_FIELDS))
            )
        for spec in BOOK_FILTER_SPECS:
            value = filters.get(spec.name)
            if value is not None and not _fits(value, spec.type):
                raise ValueError(f"filter {spec.name} must be {spec.type.__name__}")
    if sort is not None and sort not in BOOK_SORT_FIELDS:
        raise ValueError("unknown sort field; allowed: " + ", ".join(BOOK_SORT_FIELDS))
    if order not in _ORDERS:
        raise ValueError("order must be asc or desc")


def shape_books(
    books: list[dict[str, Any]],
    filters: dict[str, Any] | None,
    sort: str | None,
    order: str | None,
) -> list[dict[str, Any]]:
    """Filters, then sorts, a list of response dicts, the way the live routes
    do. With no filter and no sort the list is returned in the order it came."""
    books = filter_dicts(books, filters or {})
    return sort_dicts(books, sort, order, BOOK_SORT_FIELDS)
