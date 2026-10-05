"""
The write side of local storage: normalized Audible responses in, rows out,
on SQLite or Postgres, under the rule that Libex never accepts less than it
already holds.

Every function takes a session first, reads no settings or environment, and
raises on failure. None commits; the caller owns the transaction, and on
SQLite wraps it in `exclusive_write`.

Like `libex_core.storage`, importing this package imports nothing: the names
load on first use, so walking the package tree does not pull in SQLAlchemy.
"""

# Standard library
import importlib
from typing import Any

_EXPORTS = {
    "confirm_chapters": "libex_core.storage.write.entities",
    "delete_walk_result": "libex_core.storage.write.walks",
    "exclusive_write": "libex_core.storage.write.serialize",
    "resolve_author_ids": "libex_core.storage.write.books",
    "write_books": "libex_core.storage.write.books",
    "upsert_author": "libex_core.storage.write.entities",
    "upsert_genre": "libex_core.storage.write.entities",
    "upsert_narrator": "libex_core.storage.write.entities",
    "upsert_series": "libex_core.storage.write.entities",
    "write_author_profile": "libex_core.storage.write.entities",
    "write_series_profile": "libex_core.storage.write.entities",
    "write_track": "libex_core.storage.write.entities",
    "write_walk_result": "libex_core.storage.write.walks",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from libex_core.storage import require_storage

    require_storage()
    return getattr(importlib.import_module(module), name)
