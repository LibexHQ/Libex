"""
`LocalStore`: a Libex database the caller owns, on SQLite or Postgres.

One store owns one async engine. It never changes the database unless asked:
opening checks that the schema is the one this library ships and refuses
otherwise, and only an explicit `upgrade()` runs DDL. A database that has
tables but no record of this package's migrations is somebody else's, and is
refused outright: pointing the store at the hosted production database by
mistake must not be able to alter it.

The connection settings come from the URL and from nowhere else. A Postgres
connection is made with every parameter given outright, so the driver never
falls back on `PG*` variables, `~/.pgpass`, a service file or `~/.postgresql`;
a URL without a password connects without one. The one thing outside the URL
is the certificate trust store the verifying modes use, which is the system's,
and which OpenSSL lets `SSL_CERT_FILE` and `SSL_CERT_DIR` replace; whoever can
set this process's environment already controls far more than that.

The default TLS mode, `prefer`, encrypts when the server offers it but does not
verify the server, so it gives no protection against an active attacker on the
path. On a network that is not trusted use `ssl=verify-full`.

A caller who needs a connection this library cannot make (an SSH tunnel, IAM
tokens, a vault-issued password, an encrypted SQLite build) passes
`connect=` and supplies the connection itself. See `LocalStore` for what that
hands over; the schema, migration and locking guarantees do not change.

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
import inspect
import logging
import os
import sqlite3
import ssl
import stat
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from contextlib import asynccontextmanager
from pathlib import Path

# Third party
from sqlalchemy import event
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
    SchemaIntegrityError,
    SchemaState,
    head_revision,
    read_state,
    set_foreign_keys,
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
# The TLS modes a URL may name, each with the connection attempts it makes in
# order: True is an encrypted attempt, False a plain one. `disable` is plain
# only; `allow` tries plain and then encrypted; `prefer` tries encrypted and
# then plain; `require` and the two verifying modes are encrypted only and never
# fall back. Named as libpq names them; `ssl` in the query is read the way
# `sslmode` is. Which failures permit the next attempt is `_may_retry`.
_TLS_ATTEMPTS = {
    "disable": (False,),
    "allow": (False, True),
    "prefer": (True, False),
    "require": (True,),
    "verify-ca": (True,),
    "verify-full": (True,),
}
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


class StoreMigrationError(StoreError):
    """A schema change was rolled back because it broke an invariant."""


class StoreClosed(StoreError):
    """The store was closed, or was never opened."""


# ------------------------------------------------------------
# URL and file
# ------------------------------------------------------------

def _validate(url: str | URL, *, caller_connects: bool = False) -> URL:
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

    if caller_connects:
        # The caller's hook decides where and as whom to connect; a URL that
        # also said so would be a second, silently ignored, source of truth,
        # and a password in it would go unused but stay in memory.
        if parsed.host or parsed.port or parsed.username or parsed.password or parsed.database or parsed.query:
            raise StoreConfigError(
                "with a connect hook the Postgres URL must be bare: postgresql+asyncpg://"
            )
        return parsed

    if not parsed.host or not parsed.username or not parsed.database:
        # Anything left out would be filled from PG* variables and ~/.pgpass
        # by the driver, which is not a thing a library call should do quietly.
        raise StoreConfigError("a Postgres URL needs a host, a user and a database name")
    if set(parsed.query) - _POSTGRES_QUERY:
        raise StoreConfigError("a Postgres URL carries an option this library does not accept")
    if any(not isinstance(value, str) for value in parsed.query.values()):
        raise StoreConfigError("a Postgres URL option was given more than once")
    parsed = parsed.set(port=parsed.port or _POSTGRES_PORT)
    _tls_mode(parsed)
    return parsed


def _tls_mode(url: URL) -> str:
    mode = url.query.get("ssl")
    if mode is None:
        return "prefer"
    mode = mode.lower().replace("_", "-")
    if mode not in _TLS_ATTEMPTS:
        raise StoreConfigError(
            "the ssl option must be one of: " + ", ".join(sorted(_TLS_ATTEMPTS))
        )
    return mode


def _tls_context(mode: str) -> ssl.SSLContext:
    """The TLS context for a mode, built here so that the driver reads no
    client certificate, key, root or revocation file from `PG*` variables or
    `~/.postgresql`. Verifying modes trust the system store; the opportunistic
    modes and `require` encrypt without verifying, as libpq does."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if mode in ("verify-ca", "verify-full"):
        context.load_default_certs()
        context.check_hostname = mode == "verify-full"
    else:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _may_retry(encrypted: bool, exc: Exception) -> bool:
    """Whether a failed attempt may be followed by the other way of connecting.

    A password is never sent a second time because the first attempt failed to
    authenticate: `InvalidPasswordError` and every other rejection of the login
    end the connection, so a wrong password cannot cost the caller a plain
    retry. An encrypted attempt moves on only when the server itself answered
    the SSLRequest with a refusal, which asyncpg raises as a bare
    `ConnectionError` saying it "rejected SSL upgrade"; a refused or reset
    connection, a failed handshake or a certificate error stands. A plain
    attempt moves on only when the server turned it down for being unencrypted
    (an `InvalidAuthorizationSpecificationError` other than a bad password)."""
    import asyncpg

    if encrypted:
        return type(exc) is ConnectionError and "rejected SSL upgrade" in str(exc)
    return isinstance(exc, asyncpg.InvalidAuthorizationSpecificationError) and not isinstance(
        exc, asyncpg.InvalidPasswordError
    )


def _postgres_creator(url: URL):
    """An async connection factory that hands asyncpg every parameter, so it
    reads no `PG*` variable and no password, service or `~/.postgresql` file:
    the password is the URL's or empty, the session attribute, GSS library and
    Kerberos name are fixed, and TLS negotiation is the standard one. The
    connection settings come only from the URL; the one thing outside it is the
    certificate trust store the verifying modes load, which is the system's and
    which OpenSSL lets `SSL_CERT_FILE` and `SSL_CERT_DIR` replace. SQLAlchemy
    ignores the URL when a creator is given, so everything it carries is passed
    here. The default `prefer` encrypts without verifying, so it does not stop
    an active attacker; `verify-full` is the mode for untrusted networks."""
    mode = _tls_mode(url)
    attempts = _TLS_ATTEMPTS[mode]
    settings = {"application_name": url.query["application_name"]} if "application_name" in url.query else None
    gsslib = "sspi" if sys.platform == "win32" else "gssapi"

    async def create():
        import asyncpg

        context = _tls_context(mode) if True in attempts else None
        for position, encrypted in enumerate(attempts):
            try:
                return await asyncpg.connect(
                    host=url.host,
                    port=url.port,
                    user=url.username,
                    password=url.password if url.password is not None else "",
                    database=url.database,
                    ssl=context if encrypted else False,
                    direct_tls=False,
                    target_session_attrs="any",
                    krbsrvname="postgres",
                    gsslib=gsslib,
                    server_settings=settings,
                )
            except Exception as exc:
                # Only the opportunistic modes move on, and only when the
                # server refused this way of connecting, never because the
                # login failed; any other error ends it.
                if position == len(attempts) - 1 or not _may_retry(encrypted, exc):
                    raise
        raise AssertionError("unreachable")

    return create


def _hook_failure(stage: str, backend: str, name: str) -> StoreConnectionError:
    return StoreConnectionError(f"the connect hook {stage} for the {backend} database ({name})")


async def _discard(connection) -> None:
    """Closes a connection this library refuses or never took over, so it is
    not left open. Best effort: whatever closing raises is ignored, since the
    error worth reporting is the one that made it refuse."""
    try:
        closed = getattr(connection, "close", None)
        if callable(closed):
            result = closed()
            if inspect.isawaitable(result):
                await result
    except Exception:
        stop = getattr(connection, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass


class _Handoff:
    """Connections the pool has opened that SQLAlchemy has not yet finished
    setting up. SQLAlchemy runs its own setup on a new connection after the
    creator returns and does not close the connection when that setup raises,
    so each connection is held from before any setup runs and closed unless a
    listener registered after every other one confirms the setup finished.
    Each is tied to the task that made it, so one task's failure never closes
    a connection another task is still setting up."""

    def __init__(self) -> None:
        self._held: list[tuple[object, "asyncio.Task | None"]] = []

    def hold(self, connection) -> None:
        if any(held is connection for held, _ in self._held):
            return
        self._held.append((connection, asyncio.current_task()))

    def hold_adapted(self, dbapi_connection, _record=None) -> None:
        self.hold(getattr(dbapi_connection, "_connection", dbapi_connection))

    def claim(self, dbapi_connection, _record=None) -> None:
        raw = getattr(dbapi_connection, "_connection", dbapi_connection)
        self._held = [(c, t) for c, t in self._held if c is not raw]

    async def reclaim(self, *, everything: bool = False) -> None:
        task = asyncio.current_task()
        mine = [c for c, t in self._held if everything or t is task]
        self._held = [(c, t) for c, t in self._held if not (everything or t is task)]
        for connection in mine:
            await _discard(connection)


def _caller_postgres_creator(connect: Callable[[], Awaitable], handoff: _Handoff):
    """Wraps a caller's Postgres hook. Whatever the hook raises is reduced to
    its class name outside the `except`, so no chained context carries the
    driver's or the caller's message, which may hold a password."""

    async def create():
        import asyncpg

        failed = None
        try:
            connection = await connect()
        except Exception as exc:
            failed = type(exc).__name__
        if failed is not None:
            raise _hook_failure("raised", "postgresql", failed)
        if not isinstance(connection, asyncpg.Connection):
            await _discard(connection)
            raise _hook_failure("returned the wrong type", "postgresql", type(connection).__name__)
        handoff.hold(connection)
        return connection

    return create


_DBAPI_METHODS = ("cursor", "execute", "close", "create_function")


def _caller_sqlite_creator(connect: Callable[[str], "sqlite3.Connection"], path: str, handoff: _Handoff):
    """Wraps a caller's SQLite hook, which is handed the path this library has
    already vetted and returns a DB-API connection (`sqlite3`, or an encrypted
    build with the same interface). This library, not the caller, wraps it in
    the async connection, so it marks the worker thread a daemon before the
    thread starts, as SQLAlchemy does for a connection it opens itself, and
    reads `sqlite_master` before handing the connection on, so a wrong key or a
    file that is not a database fails here, with the connection closed."""

    def connector():
        failed = None
        try:
            raw = connect(path)
        except Exception as exc:
            failed = type(exc).__name__
        if failed is not None:
            raise _hook_failure("raised", "sqlite", failed)
        if not all(callable(getattr(raw, name, None)) for name in _DBAPI_METHODS):
            try:
                raw.close()
            except Exception:
                pass
            raise _hook_failure("returned the wrong type", "sqlite", type(raw).__name__)
        return raw

    def create(*_args, **_kwargs):
        async def make():
            import aiosqlite

            connection = aiosqlite.Connection(connector, iter_chunk_size=64)
            connection._thread.daemon = True
            error = None
            try:
                await connection
            except StoreConnectionError as exc:
                error = exc
            except Exception as exc:
                error = _hook_failure("raised", "sqlite", type(exc).__name__)
            if error is not None:
                # aiosqlite stops its worker on a failed connect without
                # waiting for it, and the worker answers on the event loop; wait
                # so it cannot outlive the loop that is about to close.
                await asyncio.to_thread(connection._thread.join, 5)
                raise error
            handoff.hold(connection)
            failed = None
            try:
                cursor = await connection.execute("SELECT count(*) FROM sqlite_master")
                await cursor.fetchall()
                await cursor.close()
            except Exception as exc:
                failed = type(exc).__name__
            if failed is not None:
                await handoff.reclaim()
                raise _hook_failure("returned a connection that failed its first read", "sqlite", failed)
            return connection

        return make()

    return create


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

    def __init__(self, url: str | URL, *, connect: Callable[..., Any] | None = None):
        """`connect=None` is the managed path: this library makes the
        connection from the URL.

        With a `connect` hook the caller makes it. On Postgres the hook takes
        no argument and returns an awaited `asyncpg.Connection`, and the URL
        must be bare (`postgresql+asyncpg://`). On SQLite the hook is a plain
        function, `(path) -> sqlite3.Connection`: it is called with the path
        this library has already checked (symlinks refused, and on Linux and
        macOS a new file created readable by you only) and returns a DB-API
        connection, `sqlite3` or a build with the same interface such as an
        encrypted one. This library wraps it in the async connection itself, so
        the worker thread is a daemon and a store never closed does not keep
        the interpreter from exiting. Write-ahead logging is not set on the
        path beforehand: `upgrade()` switches the file to it later, over
        connections the hook returns. Every schema, migration, locking and
        foreign-key guarantee still applies to what the hook returns.

        Before a SQLite connection is used it is read once (`sqlite_master`), so
        a wrong encryption key is a `StoreConnectionError` and the connection is
        closed; apply any `PRAGMA key` inside the hook. A connection that fails
        while SQLAlchemy sets it up is closed too.

        What the hook takes over is the caller's to get right: for Postgres the
        TLS mode and certificate verification, keeping `PG*` variables and
        `~/.pgpass` from being read, never resending a password in plain text
        after a failed encrypted attempt, `gsslib`, `krbsrvname` and
        `server_settings`. This library cannot manage or check any of it, and
        says so in the log. A hook that raises or returns the wrong type
        surfaces as `StoreConnectionError` naming only the exception class; a
        connection of the wrong type is closed first."""
        require_storage()
        if connect is not None and not callable(connect):
            raise StoreConfigError("connect must be a callable that makes a connection")
        self._url = _validate(url, caller_connects=connect is not None)
        self._postgres = self._url.drivername == _POSTGRES
        self._caller_connects = connect is not None
        if self._postgres and importlib.util.find_spec("asyncpg") is None:
            raise StorageUnavailable(
                "Postgres needs the 'postgres' extra (missing: asyncpg); "
                "install it with: pip install 'libex-core[postgres]'"
            )
        options: dict = {}
        self._handoff = _Handoff()
        if connect is None:
            if self._postgres:
                options = {"async_creator": _postgres_creator(self._url)}
        elif self._postgres:
            options = {"async_creator": _caller_postgres_creator(connect, self._handoff)}
        else:
            path = self._url.database or ":memory:"
            options = {"connect_args": {"async_creator_fn": _caller_sqlite_creator(connect, path, self._handoff)}}
        self._engine: AsyncEngine = create_async_engine(
            self._url, hide_parameters=True, echo=False, **options
        )
        if not self._postgres:
            # WAL is a property of the file, set by `upgrade()` once the file
            # is known to be ours; connecting to a database that is somebody
            # else's must not change how it journals.
            configure_sqlite(self._engine, wal=False)
        # First and last among the connect listeners: the first holds the new
        # connection before any setup runs, the last confirms every setup
        # succeeded. Whatever is still held after a failure is closed; see
        # `_Handoff`. Hook creators hold theirs too, before either runs.
        event.listen(self._engine.sync_engine, "connect", self._handoff.hold_adapted, insert=True)
        event.listen(self._engine.sync_engine, "connect", self._handoff.claim)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._opened = False
        self._closed = False
        self._disposal: "asyncio.Future[None] | None" = None
        if self._caller_connects:
            logger.info(
                "connections are supplied by the caller; "
                "TLS and credentials are not managed by libex-core"
            )

    def __repr__(self) -> str:
        return f"LocalStore(backend={self.backend!r}, connection_mode={self.connection_mode!r})"

    @property
    def backend(self) -> str:
        return "postgresql" if self._postgres else "sqlite"

    @property
    def connection_mode(self) -> str:
        """`"managed"` when this library makes the connection, `"caller"` when
        a `connect` hook does and TLS and credentials are the caller's."""
        return "caller" if self._caller_connects else "managed"

    # -- connecting -----------------------------------------------------

    async def _connect(self, *, write: bool = False):
        if self._closed:
            raise StoreClosed("the store is closed")
        try:
            connection = await self._engine.connect()
        except StoreConnectionError:
            await self._handoff.reclaim()
            raise
        except Exception as exc:
            await self._handoff.reclaim()
            raise StoreConnectionError(
                f"could not connect to the {self.backend} database ({type(exc).__name__})"
            ) from None
        if write and not self._postgres:
            try:
                await connection.execution_options(**{WRITE_OPTION: True})
            except Exception as exc:
                await connection.close()
                raise StoreConnectionError(
                    f"could not connect to the {self.backend} database ({type(exc).__name__})"
                ) from None
        return connection

    async def _read_state(self, connection) -> SchemaState:
        try:
            return await connection.run_sync(read_state)
        except Exception as exc:
            raise StoreConnectionError(
                f"could not read the schema of the {self.backend} database ({type(exc).__name__})"
            ) from None

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
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise StoreConnectionError(
                        "could not switch the database to write-ahead logging"
                    ) from None
                if time.monotonic() >= deadline:
                    logger.warning(
                        "gave up switching %s to write-ahead logging after %gs: it stayed locked",
                        _sqlite_path(self._url).name,
                        WAL_RETRY_SECONDS,
                    )
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
            return await self._read_state(connection)
        finally:
            await connection.close()

    async def upgrade(self) -> str:
        """Brings the schema to the head this library ships, creating a new
        SQLite file first, and returns the revision. The one place DDL runs.
        Refuses a foreign database before touching it, and a database from a
        newer library. The schema change is one transaction: if it fails the
        schema is as it was. On SQLite a new file, and its switch to
        write-ahead logging, come first and outside it, so a failed first
        upgrade can leave an empty database file behind."""
        path = None if self._postgres else _sqlite_path(self._url)
        existed = path is not None and os.path.lexists(path)
        if not self._postgres:
            self._file(create=True)
            _refuse(await self.status())
            await self._enable_wal()
        connection = await self._connect(write=True)
        try:
            if not self._postgres:
                # Off before the transaction begins, as SQLite ignores the
                # pragma inside one: a batch table rebuild with enforcement on
                # cascades its drop into the child rows.
                await connection.run_sync(set_foreign_keys, False)
            try:
                async with connection.begin():
                    _refuse(await self._read_state(connection))
                    await connection.run_sync(upgrade_to_head)
                    revision = await connection.run_sync(head_revision)
            except SchemaIntegrityError:
                raise StoreMigrationError(
                    "the migration was rolled back: it would have broken the database's "
                    "foreign keys"
                ) from None
            finally:
                if not self._postgres:
                    await connection.run_sync(set_foreign_keys, True)
        finally:
            await connection.close()
        logger.info(
            "upgraded the %s database to revision %s%s",
            self.backend,
            revision,
            "" if path is None else f" ({'existing' if existed else 'new'} file {path.name})",
        )
        return revision

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

    async def _release(self) -> None:
        await self._handoff.reclaim(everything=True)
        await self._engine.dispose()

    async def close(self) -> None:
        """Releases the engine. Safe to call twice, and on a store never opened;
        a second call made while the first is still releasing waits for it."""
        self._opened = False
        if self._disposal is None:
            self._closed = True
            self._disposal = asyncio.ensure_future(self._release())
        await asyncio.shield(self._disposal)

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
        try:
            async with self._sessions() as session:
                yield session
        except BaseException:
            await self._handoff.reclaim()
            raise

    @asynccontextmanager
    async def write(self) -> AsyncIterator[AsyncSession]:
        """A write session: serialised against other writers, committed when
        the block ends, rolled back if it raises."""
        self._require_open()
        try:
            async with self._sessions() as session:
                async with exclusive_write(session):
                    yield session
                    await session.commit()
        except BaseException:
            await self._handoff.reclaim()
            raise


def _switch_to_wal(sync_connection) -> None:
    cursor = sync_connection.connection.dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode")
        if str(cursor.fetchone()[0]).lower() != "wal":
            cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    """SQLite's SQLITE_BUSY (5) or SQLITE_LOCKED (6), whichever extended code."""
    code = getattr(exc, "sqlite_errorcode", None)
    return code is not None and (code & 0xFF) in (5, 6)


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
    "StoreMigrationError",
    "StoreNotInitialised",
    "StoreOutdated",
]
