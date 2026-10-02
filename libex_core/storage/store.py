"""
`LocalStore`: a Libex database the caller owns, on SQLite or Postgres.

One store owns one async engine. It never changes the database unless asked:
opening checks that the schema is the one this library ships and refuses
otherwise, and only an explicit `upgrade()` runs DDL. A database that has
tables but no record of this package's migrations is somebody else's, and is
refused outright: pointing the store at the hosted production database by
mistake must not be able to alter it.

The URL is the caller's secret. It is never logged and never placed in an
exception or in `repr`; every error raised here names what is wrong without
quoting it, and a failure to connect drops the driver's own message, which is
the one place a connection string could be echoed back.

    store = LocalStore("sqlite+aiosqlite:///libex.db")
    await store.upgrade()                  # explicit; creates the file if new
    async with LocalStore(url) as store:   # refuses an un-upgraded database
        async with store.write() as session:
            ...                            # commits when the block ends
        async with store.session() as session:
            ...                            # a read
"""

# Standard library
import asyncio
import importlib.util
import logging
import os
import sqlite3
import stat
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

# Third party
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# Local
from libex_core.storage import StorageUnavailable, require_storage
from libex_core.storage.dialect import WRITE_OPTION, configure_sqlite
from libex_core.storage.upgrade import (
    AHEAD,
    BEHIND,
    EMPTY,
    FOREIGN,
    SchemaState,
    head_revision,
    read_state,
    upgrade_to_head,
)
from libex_core.storage.write.serialize import exclusive_write

logger = logging.getLogger("libex.storage")

_SQLITE = "sqlite+aiosqlite"
_POSTGRES = "postgresql+asyncpg"
_DRIVERS = {
    "sqlite": _SQLITE,
    "sqlite+aiosqlite": _SQLITE,
    "postgresql": _POSTGRES,
    "postgresql+asyncpg": _POSTGRES,
}
# The only connection options a URL may carry. Anything else, a `plugin` above
# all (it loads code by entry point), is refused rather than passed through.
_POSTGRES_QUERY = frozenset({"ssl", "application_name"})
_POSTGRES_PORT = 5432
# How long preparing a new SQLite file keeps retrying while another connection
# holds it, in seconds.
WAL_RETRY_SECONDS = 10.0
# Windows has no O_NOFOLLOW; there the symlink check in `_check_file` is all there is.
_NO_FOLLOW = os.O_NOFOLLOW if sys.platform != "win32" else 0


class StoreError(Exception):
    """Base of every error the store raises on purpose."""


class StoreConfigError(StoreError, ValueError):
    """The URL or the file it names is not acceptable."""


class StoreConnectionError(StoreError):
    """The database could not be reached. Carries the driver's error class
    and nothing it said."""


class StoreNotInitialised(StoreError):
    """The database has no schema yet; run `upgrade()`."""


class StoreOutdated(StoreError):
    """The schema is not the one this library ships."""


class ForeignDatabase(StoreError):
    """The database has tables this library did not create."""


class StoreClosed(StoreError):
    """The store was closed, or was never opened."""


# ------------------------------------------------------------
# URL and file
# ------------------------------------------------------------

def _validate(url: str | URL) -> URL:
    if isinstance(url, str):
        try:
            parsed = make_url(url)
        except Exception:
            raise StoreConfigError("the database URL could not be parsed") from None
    elif isinstance(url, URL):
        parsed = url
    else:
        raise StoreConfigError("the database URL must be a string or a SQLAlchemy URL")

    driver = _DRIVERS.get(parsed.drivername)
    if driver is None:
        raise StoreConfigError(
            "unsupported database driver; use sqlite+aiosqlite or postgresql+asyncpg"
        )
    parsed = parsed.set(drivername=driver)

    if driver == _SQLITE:
        if parsed.query or parsed.host or parsed.port or parsed.username or parsed.password:
            raise StoreConfigError("a SQLite URL takes a file path and nothing else")
        database = parsed.database or ""
        if database.startswith("file:"):
            raise StoreConfigError("SQLite file: URIs are not accepted; give a plain path")
        if "\x00" in database:
            raise StoreConfigError("the database path is not valid")
        return parsed

    if not parsed.host or not parsed.username or not parsed.database:
        # Anything left out would be filled from PG* variables and ~/.pgpass
        # by the driver, which is not a thing a library call should do quietly.
        raise StoreConfigError("a Postgres URL needs a host, a user and a database name")
    if set(parsed.query) - _POSTGRES_QUERY:
        raise StoreConfigError("a Postgres URL carries an option this library does not accept")
    if any(not isinstance(value, str) for value in parsed.query.values()):
        raise StoreConfigError("a Postgres URL option was given more than once")
    return parsed.set(port=parsed.port or _POSTGRES_PORT)


def _is_memory(url: URL) -> bool:
    return url.get_backend_name() == "sqlite" and url.database in (None, "", ":memory:")


def _sqlite_path(url: URL) -> Path | None:
    return None if _is_memory(url) else Path(url.database or "")


def _check_file(path: Path, *, create: bool) -> bool:
    """Checks the SQLite file, creating it when asked and it is new. Returns
    whether it exists afterwards.

    A new file is made with O_EXCL and O_NOFOLLOW and mode 0600, so a path that
    is, or races to become, a link is never followed and the file is never
    readable by anyone else: a catalogue of what someone has been listening to
    is theirs. The umask is not touched. A file that already exists is left
    exactly as it is, with a warning if others can read it, because changing
    the permissions of a file the caller made is not ours to do.
    """
    if path.is_symlink():
        raise StoreConfigError("the database path is a symbolic link; give the real path")
    if path.exists():
        if not path.is_file():
            raise StoreConfigError("the database path is not a regular file")
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            logger.warning(
                "the database file %s is accessible to other users; "
                "restrict it with chmod 600",
                path.name,
            )
        return True
    if not create:
        return False
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NO_FOLLOW
    try:
        os.close(os.open(path, flags, 0o600))
    except FileExistsError:
        return _check_file(path, create=False)
    return True


# ------------------------------------------------------------
# The store
# ------------------------------------------------------------

class LocalStore:
    """A Libex database. See the module docstring for the lifecycle."""

    def __init__(self, url: str | URL):
        require_storage()
        self._url = _validate(url)
        self._postgres = self._url.drivername == _POSTGRES
        if self._postgres and importlib.util.find_spec("asyncpg") is None:
            raise StorageUnavailable(
                "Postgres needs the 'postgres' extra (missing: asyncpg); "
                "install it with: pip install 'libex-core[postgres]'"
            )
        self._engine: AsyncEngine = create_async_engine(
            self._url, hide_parameters=True, echo=False
        )
        if not self._postgres:
            # WAL is a property of the file, set by `upgrade()` once the file
            # is known to be ours; connecting to a database that is somebody
            # else's must not change how it journals.
            configure_sqlite(self._engine, wal=False)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._opened = False
        self._closed = False

    def __repr__(self) -> str:
        return f"LocalStore(backend={self.backend!r})"

    @property
    def backend(self) -> str:
        return "postgresql" if self._postgres else "sqlite"

    # -- connecting -----------------------------------------------------

    async def _connect(self, *, write: bool = False):
        if self._closed:
            raise StoreClosed("the store is closed")
        try:
            connection = await self._engine.connect()
            if write and not self._postgres:
                await connection.execution_options(**{WRITE_OPTION: True})
        except Exception as exc:
            raise StoreConnectionError(
                f"could not connect to the {self.backend} database ({type(exc).__name__})"
            ) from None
        return connection

    def _file(self, *, create: bool) -> bool:
        path = _sqlite_path(self._url)
        return True if path is None else _check_file(path, create=create)

    async def _enable_wal(self) -> None:
        """Switches a SQLite file to write-ahead logging, which readers need so
        they do not block the writer. Persistent, so done once, here, and only
        after the database has been found to be ours."""
        if _sqlite_path(self._url) is None:
            return
        # Changing the journal mode needs the file to itself for an instant,
        # and SQLite answers "locked" at once rather than waiting out the busy
        # timeout when another connection is mid-transaction. Two processes
        # preparing one new file is the case, so look first and retry.
        deadline = time.monotonic() + WAL_RETRY_SECONDS
        while True:
            connection = await self._connect()
            try:
                await connection.run_sync(_switch_to_wal)
                return
            except sqlite3.OperationalError:
                if time.monotonic() >= deadline:
                    raise StoreConnectionError(
                        "could not switch the database to write-ahead logging: it stayed locked"
                    ) from None
            finally:
                await connection.close()
            await asyncio.sleep(0.05)

    # -- schema ---------------------------------------------------------

    async def status(self) -> SchemaState:
        """Where the database stands against this library's schema. Reads
        only. A SQLite file that does not exist is `empty` and is not created."""
        if not self._postgres and not self._file(create=False):
            return SchemaState(EMPTY, None)
        connection = await self._connect()
        try:
            return await connection.run_sync(read_state)
        finally:
            await connection.close()

    async def upgrade(self) -> str:
        """Brings the schema to the head this library ships, creating a new
        SQLite file first, and returns the revision. The one place DDL runs.
        Refuses a foreign database before touching it, and a database from a
        newer library. One transaction: a failure leaves the database as it was."""
        if not self._postgres:
            self._file(create=True)
            _refuse(await self.status())
            await self._enable_wal()
        connection = await self._connect(write=True)
        try:
            async with connection.begin():
                state = await connection.run_sync(read_state)
                _refuse(state)
                await connection.run_sync(upgrade_to_head)
                return await connection.run_sync(head_revision)
        finally:
            await connection.close()

    async def open(self) -> "LocalStore":
        """Checks the schema and makes the store usable. Never upgrades."""
        state = await self.status()
        if state.state == EMPTY:
            raise StoreNotInitialised("the database has no schema yet; run an upgrade first")
        _refuse(state)
        if state.state == BEHIND:
            raise StoreOutdated("the database schema is behind this library; run an upgrade")
        self._opened = True
        return self

    async def close(self) -> None:
        """Releases the engine. Safe to call twice, and on a store never opened."""
        self._opened = False
        if not self._closed:
            self._closed = True
            await self._engine.dispose()

    async def __aenter__(self) -> "LocalStore":
        try:
            return await self.open()
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    # -- sessions -------------------------------------------------------

    def _require_open(self) -> None:
        if self._closed or not self._opened:
            raise StoreClosed("the store is not open")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A short read session. Nothing is committed; leaving the block
        releases it."""
        self._require_open()
        async with self._sessions() as session:
            yield session

    @asynccontextmanager
    async def write(self) -> AsyncIterator[AsyncSession]:
        """A write session: serialised against other writers, committed when
        the block ends, rolled back if it raises."""
        self._require_open()
        async with self._sessions() as session:
            async with exclusive_write(session):
                yield session
                await session.commit()


def _switch_to_wal(sync_connection) -> None:
    cursor = sync_connection.connection.dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode")
        if str(cursor.fetchone()[0]).lower() != "wal":
            cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def _refuse(state: SchemaState) -> None:
    if state.state == FOREIGN:
        raise ForeignDatabase(
            "the database has tables this library did not create; "
            "refusing to use it"
        )
    if state.state == AHEAD:
        raise StoreOutdated(
            "the database was made by a newer libex-core than this one; upgrade the library"
        )


__all__ = [
    "ForeignDatabase",
    "LocalStore",
    "StoreClosed",
    "StoreConfigError",
    "StoreConnectionError",
    "StoreError",
    "StoreNotInitialised",
    "StoreOutdated",
]
