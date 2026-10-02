"""
Local storage for Libex: the relational schema, on SQLite or Postgres.

This is the one part of `libex_core` that needs a database library, so it is
an opt-in extra rather than a dependency of the package:

    pip install "libex-core[storage]"    # SQLite
    pip install "libex-core[postgres]"   # SQLite and Postgres

Nothing here is imported when `libex_core` is, and importing this package does
not import SQLAlchemy either. The names below load on first use, and
`require_storage()` is how a caller finds out, with a message that names the
extra, that the libraries are not installed.
"""

# Standard library
import importlib
import importlib.util
from typing import Any

_REQUIRED = ("sqlalchemy", "aiosqlite")

_EXPORTS = {
    "Base": "libex_core.storage.base",
    "UTCDateTime": "libex_core.storage.types",
    "JSONDocument": "libex_core.storage.types",
    "Book": "libex_core.storage.models",
    "Author": "libex_core.storage.models",
    "Series": "libex_core.storage.models",
    "Narrator": "libex_core.storage.models",
    "Genre": "libex_core.storage.models",
    "Track": "libex_core.storage.models",
}

__all__ = ["StorageUnavailable", "require_storage", *_EXPORTS]


class StorageUnavailable(ImportError):
    """The storage extra is not installed."""


def require_storage() -> None:
    """Raise StorageUnavailable, naming the extra, unless the storage
    libraries can be imported. Looks the libraries up without importing them."""
    missing = [name for name in _REQUIRED if importlib.util.find_spec(name) is None]
    if missing:
        raise StorageUnavailable(
            "local storage needs the 'storage' extra "
            f"(missing: {', '.join(missing)}); install it with: "
            "pip install 'libex-core[storage]'"
        )


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    require_storage()
    return getattr(importlib.import_module(module), name)
