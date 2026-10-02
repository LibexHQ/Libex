"""
Shared sort query-parameter types for list endpoints.

The sort field enums are derived from the allow-lists (libex_core.shaping for
books, app.services.sorting for narrators), so the sortable surface is defined
once and the OpenAPI docs show exactly what clients can sort on. Both the DB router and the live (Audible-backed) routers
import these so the sort params look identical everywhere.
"""

# Standard library
from enum import Enum

# Core
from libex_core.shaping import BookSortField, SortOrder

# Services
from app.services.sorting import NARRATOR_SORT_FIELDS

# SortOrder and BookSortField are defined in libex_core.shaping and re-exported
# here so routers keep one import point.
__all__ = ["BookSortField", "NarratorSortField", "SortOrder"]

NarratorSortField = Enum(
    "NarratorSortField",
    {field: field for field in NARRATOR_SORT_FIELDS},
    type=str,
)