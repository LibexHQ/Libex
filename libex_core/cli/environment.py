"""
The only place libex_core reads the process environment.

Everything else in the package takes its settings as arguments, so an
embedder's own environment never changes what the library does. The command
line is the one caller that has no code to pass arguments from, and a proxy
URL can carry credentials that must not appear on a command line where other
processes can read it, so it comes from here. Reads happen when load_config()
is called, never at import.
"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROXY_URL_VARIABLE = "LIBEX_CORE_PROXY_URL"
ALLOW_DIRECT_EGRESS_VARIABLE = "LIBEX_CORE_ALLOW_DIRECT_EGRESS"
STORAGE_VARIABLE = "LIBEX_CORE_STORAGE"

# Printed in the man page's ENVIRONMENT section, in this order.
VARIABLES: tuple[tuple[str, str], ...] = (
    (
        PROXY_URL_VARIABLE,
        "URL of the http or https proxy that every request goes through. "
        "It may carry credentials, so it is read from the environment and "
        "never accepted as an option. Empty means unset.",
    ),
    (
        ALLOW_DIRECT_EGRESS_VARIABLE,
        "Set to 1, true, yes or on to let requests leave on this machine's "
        "own address when no proxy is set. 0, false, no, off or empty keeps "
        "that refused. A proxy, when set, is always used.",
    ),
    (
        STORAGE_VARIABLE,
        "Where the local store lives. off or empty, the default, keeps "
        "nothing. sqlite uses libex.db in this user's data directory, an "
        "absolute path uses that SQLite file, and a sqlite:/// or "
        "postgresql:// URL uses that database. It may carry credentials, so "
        "it is read from the environment, never accepted as an option, and "
        "never printed. Needs the storage extra, and the postgres extra for "
        "postgresql.",
    ),
)

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})


class ConfigError(Exception):
    """Raised with fixed text only: the message never contains a value read
    from the environment, since one of them is a credential."""


@dataclass(frozen=True)
class StorageTarget:
    """Where the store is: a SQLite file, or a database URL. Exactly one is
    set. Both are kept out of repr and comparison, as the URL may carry a
    password and the path says where someone's listening history is."""

    path: str | None = field(default=None, repr=False, compare=False)
    url: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Config:
    # Excluded from repr and comparison so a stray log line or assertion
    # failure cannot print the credential it may carry.
    proxy_url: str | None = field(repr=False, compare=False)
    allow_direct_egress: bool
    storage: StorageTarget | None = field(default=None, repr=False, compare=False)


_URL_SCHEMES = ("sqlite:", "sqlite+aiosqlite:", "postgresql:", "postgresql+asyncpg:")
_STORAGE_INVALID = (
    f"{STORAGE_VARIABLE} must be off, sqlite, an absolute path to a SQLite "
    "file, or a sqlite:/// or postgresql:// URL"
)


def default_database_path() -> Path:
    """The SQLite file `sqlite` stands for, under the platform's per-user data
    directory. Standard library only, so no helper package is needed."""
    try:
        home = Path.home()
    except (RuntimeError, KeyError):
        raise ConfigError(
            f"{STORAGE_VARIABLE}=sqlite needs a home directory, which could "
            "not be determined; give an absolute path instead"
        ) from None
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        data_home = os.environ.get("XDG_DATA_HOME", "")
        # The XDG rule: a relative value is invalid and is ignored.
        base = Path(data_home) if os.path.isabs(data_home) else home / ".local" / "share"
    return base / "libex-core" / "libex.db"


def _storage_target(raw: str) -> StorageTarget | None:
    value = raw.strip()
    lowered = value.lower()
    if lowered in ("", "off"):
        return None
    if lowered == "sqlite":
        return StorageTarget(path=str(default_database_path()))
    if lowered.startswith(_URL_SCHEMES):
        if lowered.startswith("sqlite"):
            rest = value.partition("://")[2]
            if rest in ("", "/:memory:", ":memory:"):
                raise ConfigError(
                    f"{STORAGE_VARIABLE} cannot be an in-memory database: "
                    "nothing would outlive the command"
                ) from None
        return StorageTarget(url=value)
    if os.path.isabs(value):
        return StorageTarget(path=value)
    raise ConfigError(_STORAGE_INVALID) from None


def load_config() -> Config:
    raw_allow = os.environ.get(ALLOW_DIRECT_EGRESS_VARIABLE, "")
    normalized = raw_allow.strip().lower()
    if normalized in _TRUE:
        allow = True
    elif normalized in _FALSE:
        allow = False
    else:
        # Checked even when a proxy is set, so a typo is found on the first
        # run rather than the day the proxy is removed.
        raise ConfigError(
            f"{ALLOW_DIRECT_EGRESS_VARIABLE} must be one of 1, true, yes, on, "
            "0, false, no, off, or empty"
        ) from None
    proxy_url = os.environ.get(PROXY_URL_VARIABLE) or None
    storage = _storage_target(os.environ.get(STORAGE_VARIABLE, ""))
    return Config(proxy_url=proxy_url, allow_direct_egress=allow, storage=storage)
