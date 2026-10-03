"""
Choosing one stored row when an ASIN is stored under more than one region.

Books and series are identified by (asin, region), so the same ASIN can have
a row per marketplace. A reader that is not told a region still has to answer
with one record per ASIN, and the answer has to be the same one every time:
the first-stored row, which the writer marks `is_primary` when it inserts it.

The mark is stored rather than worked out at read time on purpose. Ranking the
rows of the whole table per ASIN cannot be estimated by the planner, which
chose a probe per row of the table and held an unfiltered, sorted list past
the statement timeout. A flag is a plain filter with a known selectivity, and
it also settles the case the stored timestamps cannot: rows written in one
batch share a created_at, and the flag records the order they were inserted in.
"""

# Third party
from sqlalchemy import Select, Text, cast

# Local
from libex_core.storage.models import Book

# Postgres caps a single statement at 32767 bind parameters; an IN list is cut
# at the size the rest of the codebase already uses for the same reason.
IN_CHUNK = 5000


def first_stored_order(model):
    """
    ORDER BY terms for a lookup of one ASIN with no region: the primary row
    first, and should an ASIN somehow hold none (its primary row deleted by
    hand), the earliest-stored row by created_at and region code. Region codes
    are compared as text: a native Postgres enum would otherwise order by
    declaration, and SQLite has no enum at all.
    """
    return (model.is_primary.desc(), model.created_at.asc(), cast(model.region, Text).asc())


def only_first_stored(stmt: Select, model=Book) -> Select:
    """
    Narrows a select over `model` to the primary row of each ASIN.

    A filter on the statement therefore tests the primary row: an ASIN whose
    primary row fails it is not listed, even if another region's row would
    pass, so a list never returns a record different from the one a lookup of
    the same ASIN gives.
    """
    return stmt.where(model.is_primary.is_(True))
