"""
Shared sorting helpers for DB-backed list endpoints.

Each resource declares an allow-list mapping API field names (camelCase, as
clients see them) to the SQLAlchemy column to sort on. A query is sorted by
passing the requested field + direction through apply_sort, which validates
against the allow-list and appends ORDER BY before pagination.

Keeping the allow-list per resource means clients can only sort on fields that
make sense, and the field names match what the API returns.
"""

# Third party
from sqlalchemy import Select

# Database
from app.db.models import Book

# Allow-list for Book sorting: API field name -> sortable column.
# Text-heavy fields (description, summary) are intentionally excluded.
BOOK_SORT_FIELDS = {
    "title": Book.title,
    "releaseDate": Book.release_date,
    "rating": Book.rating,
    "lengthMinutes": Book.length_minutes,
    "language": Book.language,
    "publisher": Book.publisher,
    "updatedAt": Book.updated_at,
}


def apply_sort(
    stmt: Select,
    sort: str | None,
    order: str | None,
    allowed: dict,
) -> Select:
    """
    Applies ORDER BY to a select statement.

    - sort: API field name; must be a key in `allowed`. If None, the statement
      is returned unchanged (preserving the endpoint's existing default order).
    - order: "asc" or "desc" (defaults to "asc" when sort is given).
    - allowed: the resource's {api_field: column} allow-list.

    Unknown sort fields are ignored (statement returned unchanged) rather than
    raising, so a bad value never 500s — the route layer validates via enum.
    """
    if not sort:
        return stmt

    column = allowed.get(sort)
    if column is None:
        return stmt

    direction = (order or "asc").lower()
    if direction == "desc":
        return stmt.order_by(column.desc().nulls_last())
    return stmt.order_by(column.asc().nulls_last())