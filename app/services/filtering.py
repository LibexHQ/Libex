"""
Live-list filtering, re-exported from libex_core.shaping.

The DB layer filters in Postgres via WHERE clauses (app/services/db/filtering.py).
Live endpoints get their books back already assembled as response dicts and
filter them in Python; that logic lives in libex_core so it can be embedded
without the web application.
"""

# Core
from libex_core.shaping import BOOK_FILTER_FIELDS, filter_dicts

__all__ = ["BOOK_FILTER_FIELDS", "filter_dicts"]
