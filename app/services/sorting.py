"""
Shared sorting helpers for list endpoints, both DB-backed and live.

Each resource declares one allow-list mapping API field names (camelCase, as
clients see them) to the SQLAlchemy column used for DB sorting. The allow-list
serves both layers:

- DB endpoints use apply_sort, which sorts a SELECT via ORDER BY using the
  mapped column. The allow-lists and apply_sort live in
  libex_core.storage.sorting, shared with the embedded store.
- Live (Audible-backed) endpoints use sort_dicts, which sorts an already-built
  list of response dicts by the field key (libex_core.shaping) — it only needs
  the allowed field names, which are the same allow-list's keys.

Keeping one allow-list per resource means the sortable surface is defined once,
the field names match what the API returns, and clients can only sort on
fields that make sense.
"""

# Core
from libex_core.shaping import sort_dicts
from libex_core.storage.sorting import BOOK_SORT_FIELDS, NARRATOR_SORT_FIELDS, apply_sort

__all__ = ["BOOK_SORT_FIELDS", "NARRATOR_SORT_FIELDS", "apply_sort", "sort_dicts"]
