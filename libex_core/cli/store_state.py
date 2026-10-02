"""
What the command line says when the local store cannot be used, and the
errors that carry it. Light on purpose: the exit-status mapping imports this,
and it must work when the storage extra is not installed.

Every message is fixed text. None repeats the value of LIBEX_CORE_STORAGE or
any part of it.
"""

from libex_core.cli.environment import STORAGE_VARIABLE

STORAGE_OFF = (
    f"local storage is off: set {STORAGE_VARIABLE} to sqlite, an absolute path "
    "to a SQLite file, or a postgresql URL to use it (needs "
    "pip install 'libex-core[storage]')"
)

# Schema state -> (name shown by `db status`, message when it blocks a command).
# The state names are the library's; the names shown are this tool's contract.
OK = "ok"
NOT_INITIALISED = "not-initialised"
FOREIGN = "foreign"
OUTDATED = "outdated"
AHEAD = "ahead"

MESSAGES: dict[str, str] = {
    NOT_INITIALISED: "the local store has no schema yet; run: libex-core db upgrade",
    OUTDATED: "the local store's schema is behind this version; run: libex-core db upgrade",
    AHEAD: "the local store was made by a newer libex-core than this one; upgrade libex-core",
    FOREIGN: (
        "the database holds tables that libex-core did not create, so it was "
        f"left alone; point {STORAGE_VARIABLE} at a different one"
    ),
}

FILESYSTEM = (
    "the local store's file or directory could not be read, created or "
    "written; check that its location exists and is writable by this user"
)


class StoreNotReady(Exception):
    """The store exists in configuration but cannot be used yet. Fixed text."""
