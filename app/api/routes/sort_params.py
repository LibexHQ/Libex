"""
Shared sort query-parameter types for list endpoints.

The sort field enums are derived from the allow-lists in app.services.sorting,
so the sortable surface is defined once and the OpenAPI docs show exactly what
clients can sort on. Both the DB router and the live (Audible-backed) routers
import these so the sort params look identical everywhere.
"""

# Standard library
from enum import Enum

# Services
from app.services.sorting import BOOK_SORT_FIELDS, NARRATOR_SORT_FIELDS


class SortOrder(str, Enum):
    asc = "asc"
    desc = "desc"


# Built from the sort allow-lists so the enum members are exactly the
# sortable field names — no drift between what's allowed and what's offered.
BookSortField = Enum(
    "BookSortField",
    {field: field for field in BOOK_SORT_FIELDS},
    type=str,
)


NarratorSortField = Enum(
    "NarratorSortField",
    {field: field for field in NARRATOR_SORT_FIELDS},
    type=str,
)