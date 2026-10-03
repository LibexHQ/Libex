"""
`LocalStore(url, connect=...)`: the caller supplies the connection. The managed
path is unchanged when no hook is given, a hook is held to the same URL, file
and schema rules, and nothing a hook says or holds leaks through an error, a
log line or `repr`.
"""

# Standard library
import functools
import logging
import sqlite3
import ssl

# Third party
import asyncpg
import pytest
from sqlalchemy import text

# Local
from libex_core.storage import store as store_module
from libex_core.storage.store import (
    ForeignDatabase,
    LocalStore,
    StoreConfigError,
    StoreConnectionError,
)

SENTINEL = "SENTINEL-p4ssw0rd"
BARE = "postgresql+asyncpg://"
HOOK_LOG = "connections are supplied by the caller; TLS and credentials are not managed by libex-core"


def _url(path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def _noop():
    raise AssertionError("not called")


class _Fake:
    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return "connection"


# ------------------------------------------------------------
# No hook: the managed path
# ------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [{}, {"connect": None}], ids=["omitted", "none"])
async def test_without_a_hook_postgres_connects_with_the_hardened_parameters(monkeypatch, kwargs):
    fake = _Fake()
    monkeypatch.setattr(asyncpg, "connect", fake)
    engine_options = {}
    real_engine = store_module.create_async_engine

    def capture(url, **options):
        engine_options.update(options)
        return real_engine(url, **options)

    monkeypatch.setattr(store_module, "create_async_engine", capture)
    store = LocalStore("postgresql+asyncpg://u:pw@db.example:6543/d?ssl=require&application_name=app", **kwargs)
    try:
        assert store.connection_mode == "managed"
        assert "connection_mode='managed'" in repr(store)
        # The creator LocalStore handed its engine, not one built beside it.
        assert "async_creator" in engine_options
        await engine_options["async_creator"]()
    finally:
        await store.close()
    (call,) = fake.calls
    context = call.pop("ssl")
    assert isinstance(context, ssl.SSLContext)
    assert call == {
        "host": "db.example",
        "port": 6543,
        "user": "u",
        "password": "pw",
        "database": "d",
        "direct_tls": False,
        "target_session_attrs": "any",
        "krbsrvname": "postgres",
        "gsslib": "gssapi",
        "server_settings": {"application_name": "app"},
    }


async def test_omitting_the_hook_and_passing_none_build_the_same_engine(monkeypatch):
    built = []
    real = store_module._postgres_creator

    def spy(url):
        built.append(url)
        return real(url)

    monkeypatch.setattr(store_module, "_postgres_creator", spy)
    for kwargs in ({}, {"connect": None}):
        store = LocalStore("postgresql+asyncpg://u:pw@db.example/d", **kwargs)
        await store.close()
    assert len(built) == 2 and built[0] == built[1]


# ------------------------------------------------------------
# A hook and a URL
# ------------------------------------------------------------

@pytest.mark.parametrize("url", [
    f"postgresql+asyncpg://u:{SENTINEL}@h/d",
    "postgresql+asyncpg://u@h/d",
    "postgresql+asyncpg://h/d",
    "postgresql+asyncpg://h:5433",
    "postgresql+asyncpg:///d",
    "postgresql+asyncpg://?ssl=require",
    "postgresql+asyncpg://?application_name=x",
    f"postgresql+asyncpg://:{SENTINEL}@",
])
def test_a_hook_with_a_postgres_url_that_says_anything_is_refused(url):
    with pytest.raises(StoreConfigError) as caught:
        LocalStore(url, connect=_noop)
    assert SENTINEL not in str(caught.value) and SENTINEL not in repr(caught.value)


@pytest.mark.parametrize("url", [BARE, "postgresql://"])
async def test_a_bare_postgres_url_is_accepted_with_a_hook(url):
    store = LocalStore(url, connect=_noop)
    try:
        assert store.connection_mode == "caller"
        assert store.backend == "postgresql"
    finally:
        await store.close()


@pytest.mark.parametrize("hook", [object(), "dsn", 3, b"x"])
@pytest.mark.parametrize("url", [BARE, "sqlite+aiosqlite:///x.db"])
def test_a_hook_that_is_not_callable_is_refused(url, hook):
    with pytest.raises(StoreConfigError, match="callable"):
        LocalStore(url, connect=hook)


def test_the_hook_is_keyword_only():
    with pytest.raises(TypeError):
        LocalStore(BARE, _noop)  # type: ignore[misc]


# ------------------------------------------------------------
# Nothing the hook holds or says leaks
# ------------------------------------------------------------

def _hook_that_raises(secret):
    async def make(secret):
        raise Exception(secret)

    return functools.partial(make, secret)


async def test_a_postgres_hook_that_raises_leaks_nothing(caplog):
    caplog.set_level(logging.DEBUG)
    store = LocalStore(BARE, connect=_hook_that_raises(SENTINEL))
    try:
        assert SENTINEL not in repr(store) and SENTINEL not in str(store)
        with pytest.raises(StoreConnectionError) as caught:
            await store.status()
    finally:
        await store.close()
    error = caught.value
    assert "Exception" in str(error)
    assert SENTINEL not in str(error) and SENTINEL not in repr(error)
    assert all(SENTINEL not in repr(arg) for arg in error.args)
    assert error.__cause__ is None
    assert error.__context__ is None or SENTINEL not in repr(error.__context__)
    assert SENTINEL not in caplog.text
    assert all(SENTINEL not in record.getMessage() for record in caplog.records)


async def test_a_postgres_hook_returning_the_wrong_type_is_a_connection_error():
    async def wrong():
        return SENTINEL

    store = LocalStore(BARE, connect=wrong)
    try:
        with pytest.raises(StoreConnectionError) as caught:
            await store.status()
    finally:
        await store.close()
    assert "str" in str(caught.value)
    assert SENTINEL not in str(caught.value)


async def test_a_postgres_hook_that_is_not_awaitable_is_a_connection_error():
    store = LocalStore(BARE, connect=lambda: SENTINEL)
    try:
        with pytest.raises(StoreConnectionError) as caught:
            await store.status()
    finally:
        await store.close()
    assert SENTINEL not in str(caught.value)


async def test_a_sqlite_hook_that_raises_or_returns_the_wrong_type_leaks_nothing(tmp_path):
    path = tmp_path / "libex.db"
    path.touch()

    def raises(_path):
        raise Exception(SENTINEL)

    def wrong(_path):
        return SENTINEL

    for hook, name in ((raises, "Exception"), (wrong, "str")):
        store = LocalStore(_url(path), connect=hook)
        try:
            with pytest.raises(StoreConnectionError) as caught:
                await store.status()
        finally:
            await store.close()
        assert name in str(caught.value) and SENTINEL not in str(caught.value)
        assert caught.value.__cause__ is None


async def test_the_hook_is_not_kept_where_repr_could_reach_it(caplog):
    caplog.set_level(logging.INFO, logger="libex")
    hook = _hook_that_raises(SENTINEL)
    store = LocalStore(BARE, connect=hook)
    try:
        assert repr(store) == "LocalStore(backend='postgresql', connection_mode='caller')"
        assert not any(value is hook for value in vars(store).values())
    finally:
        await store.close()
    assert any(r.getMessage() == HOOK_LOG for r in caplog.records)
    assert SENTINEL not in caplog.text and repr(hook) not in caplog.text


async def test_a_managed_store_logs_no_hook_notice(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="libex")
    store = LocalStore(_url(tmp_path / "libex.db"))
    await store.close()
    assert not any("supplied by the caller" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------
# SQLite: the same file, schema and connection rules with a hook
# ------------------------------------------------------------

async def test_a_sqlite_hook_receives_the_vetted_path_and_the_store_works(tmp_path):
    path = tmp_path / "sub" / "libex.db"
    seen = []

    def connect(received):
        seen.append(received)
        return sqlite3.connect(received)

    store = LocalStore(_url(path), connect=connect)
    try:
        await store.upgrade()
        await store.open()
        async with store.session() as session:
            assert (await session.execute(text("PRAGMA foreign_keys"))).scalar() == 1
            assert (await session.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
            assert (await session.execute(text("PRAGMA busy_timeout"))).scalar() > 0
    finally:
        await store.close()
    assert seen and set(seen) == {str(path)}
    assert (path.stat().st_mode & 0o077) == 0
    assert store.connection_mode == "caller"


async def test_a_symlink_is_refused_before_a_sqlite_hook_is_called(tmp_path):
    target = tmp_path / "elsewhere.db"
    link = tmp_path / "link.db"
    link.symlink_to(target)
    called = []

    def connect(path):
        called.append(path)
        return sqlite3.connect(path)

    store = LocalStore(_url(link), connect=connect)
    try:
        with pytest.raises(StoreConfigError, match="symbolic link"):
            await store.upgrade()
        with pytest.raises(StoreConfigError, match="symbolic link"):
            await store.status()
    finally:
        await store.close()
    assert called == [] and not target.exists()


async def test_a_foreign_sqlite_database_is_refused_with_a_hook(tmp_path):
    path = tmp_path / "other.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE books (id INTEGER)")
    store = LocalStore(_url(path), connect=lambda p: sqlite3.connect(p))
    try:
        with pytest.raises(ForeignDatabase):
            await store.upgrade()
        with pytest.raises(ForeignDatabase):
            await store.open()
    finally:
        await store.close()
