"""
Choosing one stored row when an ASIN is stored under more than one region.

Books and series are identified by (asin, region), so the same ASIN can have
a row per marketplace. A reader that is not told a region still has to answer
with one record per ASIN, and the answer has to be the same one every time:
the first-stored row, which is the lowest created_at and, for rows written in
the same instant, the lowest region code.

Region codes are compared as text. A native Postgres enum would otherwise
order by declaration, not alphabetically, and SQLite has no enum at all, so
the tie-break would differ by backend.
"""

# Third party
from sqlalchemy import Select, Text, cast, func, select, tuple_

# Local
from libex_core.storage.models import Book

# Postgres caps a single statement at 32767 bind parameters; an IN list is cut
# at the size the rest of the codebase already uses for the same reason.
IN_CHUNK = 5000


def first_stored_order(model):
    """ORDER BY terms that put a model's first-stored row first."""
    return (model.created_at.asc(), cast(model.region, Text).asc())


def only_first_stored(stmt: Select, model=Book) -> Select:
    """
    Narrows a select over `model` to the first-stored row per ASIN among the
    rows it already selects.

    The ranking runs over the rows that pass the statement's own filters, so
    a book whose first-stored row fails a filter another region's row passes
    is still found, by the row that passes. Call it on the statement before
    loader options, ordering and paging are added: those apply to the rows
    that survive.
    """
    ranked = (
        stmt.with_only_columns(
            model.asin,
            model.region,
            func.row_number()
            .over(partition_by=model.asin, order_by=first_stored_order(model))
            .label("rn"),
        )
        .order_by(None)
        .subquery()
    )
    first = select(ranked.c.asin, ranked.c.region).where(ranked.c.rn == 1)
    return stmt.where(tuple_(model.asin, model.region).in_(first))
