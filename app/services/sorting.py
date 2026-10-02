"""
Shared sorting helpers for list endpoints, both DB-backed and live.

Each resource declares one allow-list mapping API field names (camelCase, as
clients see them) to the SQLAlchemy column used for DB sorting. The allow-list
serves both layers:

- DB endpoints use apply_sort, which sorts a SELECT via ORDER BY using the
  mapped column.
- Live (Audible-backed) endpoints use sort_dicts, which sorts an already-built
  list of response dicts by the field key (libex_core.shaping) — it only needs
  the allowed field names, which are the same allow-list's keys.

Keeping one allow-list per resource means the sortable surface is defined once,
the field names match what the API returns, and clients can only sort on
fields that make sense.
"""

# Third party
from sqlalchemy import Select

# Core
from libex_core.shaping import BOOK_SORT_FIELDS as CORE_BOOK_SORT_FIELDS
from libex_core.shaping import sort_dicts

# Database
from app.db.models import Book, Narrator

# Allow-list for Book sorting: API field name -> sortable column.
# The field names come from libex_core.shaping (shared with live sorting);
# only the column mapping is a database concern and stays here.
_BOOK_SORT_COLUMNS = {
    "title": Book.title,
    "releaseDate": Book.release_date,
    "rating": Book.rating,
    "lengthMinutes": Book.length_minutes,
    "language": Book.language,
    "publisher": Book.publisher,
    "updatedAt": Book.updated_at,
}
BOOK_SORT_FIELDS = {field: _BOOK_SORT_COLUMNS[field] for field in CORE_BOOK_SORT_FIELDS}

# Allow-list for Narrator sorting. Only scalar fields that sort sensibly.
# audiobooksProduced is excluded — it holds categorical buckets ("1 to 10",
# "More than 100"), so it belongs in filtering, not sorting.
NARRATOR_SORT_FIELDS = {
    "name": Narrator.name,
    "source": Narrator.source,
    "sourceUpdatedAt": Narrator.source_updated_at,
    "updatedAt": Narrator.updated_at,
}


def apply_sort(
    stmt: Select,
    sort: str | None,
    order: str | None,
    allowed: dict,
) -> Select:
    """
    Applies ORDER BY to a select statement (DB-backed endpoints).

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


__all__ = ["BOOK_SORT_FIELDS", "NARRATOR_SORT_FIELDS", "apply_sort", "sort_dicts"]
