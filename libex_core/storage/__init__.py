"""
Local storage for Libex: the relational schema, on SQLite or Postgres.

This is the one part of `libex_core` that needs a database library, so it is
an opt-in extra rather than a dependency of the package:

    pip install "libex-core[sqlite]"     # SQLite
    pip install "libex-core[postgres]"   # Postgres

`libex-core[storage]` is the older name for `[sqlite]` and still installs the
same thing.

Nothing here is imported when `libex_core` is, and importing this package does
not import SQLAlchemy either. The names below load on first use, and
`require_storage()` is how a caller finds out, with a message that names the
extra, that the libraries are not installed.
"""

# Standard library
import importlib
import importlib.util
from typing import Any

# What every backend needs. The driver is checked where the backend is known,
# when a store resolves its URL.
_REQUIRED = ("sqlalchemy", "alembic")

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
    "LocalStore": "libex_core.storage.store",
    "StoreError": "libex_core.storage.store",
    "StoreConfigError": "libex_core.storage.store",
    "StoreConnectionError": "libex_core.storage.store",
    "StoreNotInitialised": "libex_core.storage.store",
    "StoreOutdated": "libex_core.storage.store",
    "ForeignDatabase": "libex_core.storage.store",
    "StoreClosed": "libex_core.storage.store",
    "StoreMigrationError": "libex_core.storage.store",
}

__all__ = ["StorageUnavailable", "require_storage", *_EXPORTS]


class StorageUnavailable(ImportError):
    """The libraries local storage needs are not installed."""


def require_storage() -> None:
    """Raise StorageUnavailable, naming the extras, unless the libraries every
    backend shares can be imported. Looks them up without importing them. The
    SQLite or Postgres driver is checked by the store that uses it."""
    missing = [name for name in _REQUIRED if importlib.util.find_spec(name) is None]
    if missing:
        raise StorageUnavailable(
            "local storage needs the 'sqlite' or 'postgres' extra "
            f"(missing: {', '.join(missing)}); install it with: "
            "pip install 'libex-core[sqlite]' or pip install 'libex-core[postgres]'"
        )


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    require_storage()
    return getattr(importlib.import_module(module), name)
