"""A SQLite connect hook's connection is wrapped by this library: the
interpreter exits though the store is never closed, a connection that fails
the first read (a wrong key) or the pool's own setup is closed, and a
connection of the wrong type is refused and closed."""

# Standard library
import subprocess
import sys
import textwrap

# Third party
import pytest

# Local
from libex_core.storage import store as store_module
from libex_core.storage.store import LocalStore, StoreConnectionError

pytest.importorskip("aiosqlite")

_SCRIPT = textwrap.dedent(
    """
    import asyncio, sqlite3, sys
    from libex_core.storage.store import LocalStore

    def hook(path):
        return sqlite3.connect(path)

    async def main():
        url = "sqlite+aiosqlite:///" + sys.argv[1]
        store = LocalStore(url, connect=hook)
        await store.upgrade()
        await store.open()
        async with store.session():
            pass
        # never closed

    asyncio.run(main())
    """
)


def test_a_hook_store_that_is_never_closed_still_exits(tmp_path):
    result = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", _SCRIPT, str(tmp_path / "x.db")],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_the_worker_thread_is_a_daemon_before_it_starts(tmp_path, monkeypatch):
    import aiosqlite

    seen = []
    real = aiosqlite.Connection.__await__

    def spy(self):
        seen.append(self._thread.daemon)
        return real(self)

    monkeypatch.setattr(aiosqlite.Connection, "__await__", spy)

    async def go():
        store = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}", connect=_sqlite)
        try:
            await store.upgrade()
        finally:
            await store.close()

    import asyncio

    asyncio.run(go())
    assert seen and all(seen)


class _Real:
    """A sqlite3 connection that records being closed and can fail."""

    def __init__(self, inner, *, fail_on=None):
        self._inner = inner
        self._fail_on = fail_on
        self.closed = False

    def cursor(self, *a, **k):
        return self._inner.cursor(*a, **k)

    def execute(self, sql, *a):
        if self._fail_on and self._fail_on in sql:
            raise RuntimeError("secret-password")
        return self._inner.execute(sql, *a)

    def create_function(self, *a, **k):
        if self._fail_on == "create_function":
            raise RuntimeError("secret-password")
        return self._inner.create_function(*a, **k)

    def close(self):
        self.closed = True
        self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        if name in ("_inner", "_fail_on", "closed"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)


def _sqlite(path):
    import sqlite3

    return sqlite3.connect(path)


def _store(tmp_path, made, fail_on):
    def hook(path):
        import sqlite3

        wrapped = _Real(sqlite3.connect(path, check_same_thread=False), fail_on=fail_on)
        made.append(wrapped)
        return wrapped

    return LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}", connect=hook)


async def test_a_connection_that_fails_its_first_read_is_closed(tmp_path):
    made = []
    store = _store(tmp_path, made, "sqlite_master")
    with pytest.raises(StoreConnectionError) as caught:
        await store.upgrade()
    await store.close()
    assert made and all(c.closed for c in made)
    message = str(caught.value)
    assert "RuntimeError" in message and "secret-password" not in message
    assert caught.value.__cause__ is None


async def test_a_connection_that_fails_the_pool_setup_is_closed(tmp_path):
    # Past the read, the pool's own listener runs and fails; the connection
    # must still be closed, by the store, not left to the garbage collector.
    made = []
    store = _store(tmp_path, made, "create_function")
    with pytest.raises(StoreConnectionError):
        await store.upgrade()
    # Closed by the failed connect itself, before the store is closed.
    assert made and all(c.closed for c in made)
    await store.close()


async def test_closing_the_store_closes_a_connection_that_was_never_confirmed(tmp_path):
    closed = []

    class Conn:
        async def close(self):
            closed.append(1)

    store = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    store._handoff.hold(Conn())
    await store.close()
    assert closed == [1]


async def test_a_connection_that_is_not_a_database_connection_is_refused_and_closed(tmp_path):
    closed = []

    class Wrong:
        def close(self):
            closed.append(1)

    store = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}", connect=lambda path: Wrong())
    with pytest.raises(StoreConnectionError) as caught:
        await store.upgrade()
    await store.close()
    assert closed == [1] and "Wrong" in str(caught.value)


async def test_a_pending_connection_is_closed_on_a_failed_connect_and_a_confirmed_one_is_kept(tmp_path):
    handoff = store_module._Handoff()
    closed = []

    class Conn:
        def __init__(self, name):
            self.name = name

        async def close(self):
            closed.append(self.name)

    a, b = Conn("a"), Conn("b")
    handoff.hold(a)
    handoff.hold(b)
    handoff.claim(b)
    await handoff.reclaim()
    assert closed == ["a"]


class _Wrong:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _Unclosable:
    async def close(self):
        raise RuntimeError("secret-password")


async def test_wrong_type_postgres_connection_is_closed():
    pytest.importorskip("asyncpg")
    wrong = _Wrong()

    async def hook():
        return wrong

    store = LocalStore("postgresql+asyncpg://", connect=hook)
    with pytest.raises(StoreConnectionError):
        await store.upgrade()
    await store.close()
    assert wrong.closed


async def test_failure_to_close_a_refused_connection_is_not_reported():
    pytest.importorskip("asyncpg")

    async def hook():
        return _Unclosable()

    store = LocalStore("postgresql+asyncpg://", connect=hook)
    with pytest.raises(StoreConnectionError) as caught:
        await store.upgrade()
    await store.close()
    assert "secret-password" not in str(caught.value)
    assert "RuntimeError" not in str(caught.value)


async def test_a_postgres_connection_that_fails_the_pool_setup_is_closed(monkeypatch):
    asyncpg = pytest.importorskip("asyncpg")
    from unittest import mock

    connection = mock.create_autospec(asyncpg.Connection, instance=True)
    closed = []

    async def close():
        closed.append(1)

    connection.close = close

    async def hook():
        return connection

    store = LocalStore("postgresql+asyncpg://", connect=hook)
    with pytest.raises(StoreConnectionError):
        await store.status()
    assert closed, "the held connection was never closed"


async def test_a_healthy_hook_connection_is_confirmed_and_not_left_pending(tmp_path):
    store = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}", connect=_sqlite)
    try:
        await store.upgrade()
        await store.open()
        assert store._handoff._held == []
    finally:
        await store.close()


# ------------------------------------------------------------
# The managed path: no hook, the same guarantee
# ------------------------------------------------------------

class _FailingSetup:
    """Makes the pool's own setup raise after the connection is open, by
    registering one more connect listener ahead of the store's confirming one."""

    def __init__(self, monkeypatch):
        self.armed = True
        self.closed = []
        import aiosqlite
        from sqlalchemy import event

        real_configure = store_module.configure_sqlite
        real_close = aiosqlite.Connection.close
        outer = self

        def configure(engine, **kwargs):
            real_configure(engine, **kwargs)

            @event.listens_for(engine.sync_engine, "connect")
            def fail(dbapi_connection, record):
                if outer.armed:
                    raise RuntimeError("secret-password")

        async def close(self):
            outer.closed.append(self)
            return await real_close(self)

        monkeypatch.setattr(store_module, "configure_sqlite", configure)
        monkeypatch.setattr(aiosqlite.Connection, "close", close)


async def test_a_managed_sqlite_connection_that_fails_the_pool_setup_is_closed(tmp_path, monkeypatch):
    failing = _FailingSetup(monkeypatch)
    store = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    with pytest.raises(StoreConnectionError) as caught:
        await store.upgrade()
    # Closed by the failed connect itself, before the store is closed.
    assert failing.closed
    assert "secret-password" not in str(caught.value)
    await store.close()


async def test_a_session_connection_that_fails_the_pool_setup_is_closed_at_once(tmp_path, monkeypatch):
    from sqlalchemy import text

    failing = _FailingSetup(monkeypatch)
    failing.armed = False
    store = LocalStore(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    await store.upgrade()
    await store.open()
    try:
        async with store.session() as first:
            await first.execute(text("SELECT 1"))
            failing.armed = True
            before = len(failing.closed)
            with pytest.raises(Exception):
                async with store.session() as second:
                    await second.execute(text("SELECT 1"))
            # The second session needed a new connection, whose setup failed;
            # it is closed now, while the first session is still using its own.
            assert len(failing.closed) == before + 1
            failing.armed = False
    finally:
        await store.close()


async def test_a_managed_postgres_connection_that_fails_the_pool_setup_is_closed(monkeypatch):
    asyncpg = pytest.importorskip("asyncpg")
    from unittest import mock

    connection = mock.create_autospec(asyncpg.Connection, instance=True)
    closed = []

    async def close():
        closed.append(1)

    connection.close = close

    async def connect(**kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    store = LocalStore("postgresql+asyncpg://u:pw@db.example/d")
    with pytest.raises(StoreConnectionError):
        await store.status()
    assert closed, "the managed connection was never closed"
    await store.close()
