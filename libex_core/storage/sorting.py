"""
Sort allow-lists and ORDER BY for the stored-catalog readers.

Each resource declares one allow-list mapping API field names (camelCase, as
callers see them) to the column sorted on. `apply_sort` sorts a SELECT by the
mapped column; the field names for books come from `libex_core.shaping`, the
same list live sorting uses, so the sortable surface is defined once.
"""

# Third party
from sqlalchemy import Select

# Local
from libex_core.shaping import BOOK_SORT_FIELDS as CORE_BOOK_SORT_FIELDS
from libex_core.storage.models import Book, Narrator

# Allow-list for Book sorting: API field name -> sortable column.
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
# audiobooksProduced is excluded: it holds categorical buckets ("1 to 10",
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
    Applies ORDER BY to a select statement.

    - sort: API field name; must be a key in `allowed`. If None, the statement
      is returned unchanged (preserving the endpoint's existing default order).
    - order: "asc" or "desc" (defaults to "asc" when sort is given).
    - allowed: the resource's {api_field: column} allow-list.

    Unknown sort fields are ignored (statement returned unchanged) rather than
    raising, so a bad value never 500s; callers validate against their own enum.

    NULLs sort last in both directions. Text columns order by the backend's own
    collation, so Postgres follows its database locale and SQLite compares
    bytes: the same rows can come back in a different order for mixed-case or
    accented titles.
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
