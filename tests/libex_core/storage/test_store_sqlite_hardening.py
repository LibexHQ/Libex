"""
`LocalStore` on SQLite, the parts that guard a caller's data: foreign keys
across a table-rebuilding migration, the write-ahead-logging switch and its
retry, concurrent close, and what a failure or a log line may say.
"""

# Standard library
import asyncio
import logging
import sqlite3
from types import SimpleNamespace

# Third party
import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

# Local
from libex_core.storage import StoreMigrationError
from libex_core.storage import store as store_module
from libex_core.storage.models import Author, Book, author_book
from libex_core.storage.store import (
    LocalStore,
    StoreConfigError,
    StoreConnectionError,
)
from libex_core.storage.upgrade import SchemaIntegrityError, set_foreign_keys, upgrade_to_head


def _url(path) -> str:
    return f"sqlite+aiosqlite:///{path}"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "libex.db"


@pytest.fixture(params=["managed", "caller"])
def kw(request, db):
    """The keyword arguments that open a store the library connects, and one
    the caller does: the foreign-key guarantees hold for both. In caller mode
    the hook records each path it is given, and the fixture fails the test if
    the hook never ran or was handed anything but the vetted path."""
    if request.param == "managed":
        yield {}
        return
    seen = []

    def connect(path):
        seen.append(path)
        return sqlite3.connect(path)

    yield {"connect": connect}
    assert seen, "the connect hook was never called"
    assert set(seen) == {str(db)}


def _counts(path) -> tuple[int, int, int]:
    with sqlite3.connect(path) as conn:
        return tuple(
            conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("books", "authors", "author_book")
        )


async def _seed_linked_pair(path, kw=None) -> None:
    store = LocalStore(_url(path), **(kw or {}))
    await store.upgrade()
    await store.open()
    try:
        async with store.write() as session:
            session.add(Book(asin="B000000001", title="t", region="us"))
            session.add(Author(id=1, name="a", region="us"))
            await session.flush()
            await session.execute(author_book.insert().values(author_id=1, book_asin="B000000001", book_region="us"))
    finally:
        await store.close()


def _scratch_migration(monkeypatch, *, orphan: bool = False):
    """Replaces the alembic upgrade with one scratch migration that rebuilds
    `books` the way a batch migration does, optionally orphaning a pivot row
    while it runs."""

    def rebuild(config, target):
        connection = config.attributes["connection"]
        operations = Operations(MigrationContext.configure(connection))
        with operations.batch_alter_table("books", recreate="always") as batch:
            batch.create_index("ix_scratch_title", ["title"])
        if orphan:
            connection.exec_driver_sql(
                "INSERT INTO author_book (author_id, book_asin, book_region) "
                "VALUES (999, 'B000000001', 'us')"
            )

    monkeypatch.setattr(command, "upgrade", rebuild)


async def _foreign_keys_on_a_new_connection(path, kw=None) -> int:
    async with LocalStore(_url(path), **(kw or {})) as store:
        async with store.session() as session:
            return (await session.execute(text("PRAGMA foreign_keys"))).scalar()


# ------------------------------------------------------------
# Foreign keys across a rebuild
# ------------------------------------------------------------

async def test_a_table_rebuild_keeps_the_rows_that_point_at_it(db, monkeypatch, kw):
    await _seed_linked_pair(db, kw)
    assert _counts(db) == (1, 1, 1)
    _scratch_migration(monkeypatch)
    store = LocalStore(_url(db), **kw)
    try:
        await store.upgrade()
    finally:
        await store.close()
    assert _counts(db) == (1, 1, 1)
    with sqlite3.connect(db) as conn:
        names = {r[1] for r in conn.execute("PRAGMA index_list(books)")}
    assert "ix_scratch_title" in names  # the rebuild really ran
    assert await _foreign_keys_on_a_new_connection(db, kw) == 1


async def test_a_rebuild_that_orphans_a_row_is_rolled_back_and_refused(db, monkeypatch, kw):
    await _seed_linked_pair(db, kw)
    _scratch_migration(monkeypatch, orphan=True)
    store = LocalStore(_url(db), **kw)
    try:
        with pytest.raises(StoreMigrationError) as caught:
            await store.upgrade()
    finally:
        await store.close()
    assert "rolled back" in str(caught.value)
    assert caught.value.__cause__ is None
    assert _counts(db) == (1, 1, 1)
    with sqlite3.connect(db) as conn:
        assert "ix_scratch_title" not in {r[1] for r in conn.execute("PRAGMA index_list(books)")}
        assert conn.execute("SELECT count(*) FROM author_book WHERE author_id = 999").fetchone()[0] == 0
    assert await _foreign_keys_on_a_new_connection(db, kw) == 1


async def test_the_migration_runner_refuses_to_run_with_foreign_keys_on(db, kw):
    await _seed_linked_pair(db, kw)
    async with LocalStore(_url(db), **kw) as store:
        connection = await store._connect()
        try:
            assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar() == 1
            with pytest.raises(SchemaIntegrityError, match="foreign keys off"):
                await connection.run_sync(upgrade_to_head)
        finally:
            await connection.close()
    assert _counts(db) == (1, 1, 1)


def _fake_connection(dialect: str, reads_back: int):
    cursor = SimpleNamespace(
        execute=lambda sql: None,
        fetchone=lambda: (reads_back,),
        close=lambda: None,
    )
    driver = SimpleNamespace(cursor=lambda: cursor)
    return SimpleNamespace(
        dialect=SimpleNamespace(name=dialect),
        connection=SimpleNamespace(dbapi_connection=driver),
    )


def test_a_foreign_key_switch_that_does_not_take_is_an_error():
    with pytest.raises(SchemaIntegrityError, match="could not be switched"):
        set_foreign_keys(_fake_connection("sqlite", reads_back=1), False)
    with pytest.raises(SchemaIntegrityError, match="could not be switched"):
        set_foreign_keys(_fake_connection("sqlite", reads_back=0), True)
    set_foreign_keys(_fake_connection("sqlite", reads_back=0), False)


def test_the_foreign_key_switch_does_nothing_off_sqlite():
    set_foreign_keys(SimpleNamespace(dialect=SimpleNamespace(name="postgresql")), False)


# ------------------------------------------------------------
# Switching to write-ahead logging
# ------------------------------------------------------------

async def _noop():
    return None


def _locked(code: int = 5) -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError("database is locked")
    exc.sqlite_errorcode = code
    return exc


def _count_switches(monkeypatch, errors):
    """Replaces the switch with one that raises each of `errors` in turn and
    then succeeds, and returns the list of attempts."""
    attempts = []
    pending = list(errors)

    def switch(sync_connection):
        attempts.append(1)
        if pending:
            raise pending.pop(0)

    monkeypatch.setattr(store_module, "_switch_to_wal", switch)
    return attempts


@pytest.mark.parametrize("code", [5, 6, 261, 517])
async def test_a_busy_or_locked_switch_is_retried_until_it_succeeds(db, monkeypatch, code):
    attempts = _count_switches(monkeypatch, [_locked(code), _locked(code)])
    store = LocalStore(_url(db))
    try:
        await store._enable_wal()
    finally:
        await store.close()
    assert len(attempts) == 3


async def test_a_switch_that_stays_locked_gives_up_with_the_file_name_only(db, monkeypatch, caplog):
    monkeypatch.setattr(store_module, "WAL_RETRY_SECONDS", 0.3)
    attempts = _count_switches(monkeypatch, [_locked()] * 1000)
    caplog.set_level(logging.DEBUG, logger="libex.storage")
    store = LocalStore(_url(db))
    try:
        with pytest.raises(StoreConnectionError, match="stayed locked") as caught:
            await store._enable_wal()
    finally:
        await store.close()
    assert len(attempts) >= 3  # it kept trying until the deadline, not once
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "libex.db" in message and str(db.parent) not in message
    assert str(db.parent) not in str(caught.value) and caught.value.__cause__ is None


@pytest.mark.parametrize("code", [1, 8, 14, None])
async def test_any_other_error_raises_at_once_with_a_fixed_message(db, monkeypatch, code, caplog):
    exc = sqlite3.OperationalError(f"disk I/O error at {db}")
    if code is not None:
        exc.sqlite_errorcode = code
    attempts = _count_switches(monkeypatch, [exc])
    store = LocalStore(_url(db))
    try:
        with pytest.raises(StoreConnectionError) as caught:
            await store._enable_wal()
    finally:
        await store.close()
    assert len(attempts) == 1
    assert str(caught.value) == "could not switch the database to write-ahead logging"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert str(db) not in caplog.text


async def test_a_real_lock_held_by_another_connection_is_retried_then_cleared(db):
    store = LocalStore(_url(db))
    await store.upgrade()
    await store.close()
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    store = LocalStore(_url(db))
    try:
        task = asyncio.ensure_future(store._enable_wal())
        await asyncio.sleep(0.2)
        assert not task.done()  # blocked by the lock, still trying
        holder.execute("ROLLBACK")
        await asyncio.wait_for(task, 20)
    finally:
        holder.close()
        await store.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# ------------------------------------------------------------
# Close, connect, status, upgrade
# ------------------------------------------------------------

async def test_two_concurrent_closes_both_complete_and_dispose_once(db, monkeypatch):
    await _seed_linked_pair(db)
    store = LocalStore(_url(db))
    disposed = []
    finished = []
    real = store._engine.dispose

    async def slow_dispose():
        disposed.append(1)
        await asyncio.sleep(0.1)
        await real()
        finished.append(1)

    async def close_and_check():
        await store.close()
        assert finished == [1]  # a caller never returns before the release is done

    monkeypatch.setattr(store, "_engine", SimpleNamespace(dispose=slow_dispose))
    await asyncio.wait_for(asyncio.gather(close_and_check(), close_and_check()), 5)
    assert disposed == [1]


async def test_a_failure_applying_the_write_option_closes_the_connection(db, monkeypatch):
    store = LocalStore(_url(db))
    closed = []

    class Connection:
        async def execution_options(self, **kwargs):
            raise RuntimeError(f"cannot at {db}")

        async def close(self):
            closed.append(1)

    async def connect():
        return Connection()

    monkeypatch.setattr(store, "_engine", SimpleNamespace(connect=connect, dispose=_noop))
    try:
        with pytest.raises(StoreConnectionError) as caught:
            await store._connect(write=True)
    finally:
        await store.close()
    assert closed == [1]
    assert "RuntimeError" in str(caught.value) and str(db) not in str(caught.value)
    assert caught.value.__cause__ is None


async def test_status_on_a_file_that_is_not_a_database_names_neither_path_nor_sql(db):
    db.write_bytes(b"this is not a sqlite database at all " * 200)
    store = LocalStore(_url(db))
    try:
        with pytest.raises(StoreConnectionError) as caught:
            await store.status()
    finally:
        await store.close()
    message = str(caught.value)
    assert str(db.parent) not in message and "libex.db" not in message
    for sql in ("SELECT", "PRAGMA", "sqlite_master"):
        assert sql not in message
    assert "DatabaseError" in message
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


async def test_a_failure_reading_the_schema_names_neither_path_nor_sql(db, monkeypatch):
    await _seed_linked_pair(db)

    def boom(connection):
        raise sqlite3.DatabaseError(f"malformed {db}: SELECT name FROM sqlite_master")

    monkeypatch.setattr(store_module, "read_state", boom)
    store = LocalStore(_url(db))
    try:
        with pytest.raises(StoreConnectionError) as caught:
            await store.status()
    finally:
        await store.close()
    message = str(caught.value)
    assert "DatabaseError" in message and "could not read the schema" in message
    assert str(db.parent) not in message and "SELECT" not in message
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


async def test_upgrade_logs_the_revision_and_whether_the_file_was_new_by_name_only(db, caplog):
    caplog.set_level(logging.INFO, logger="libex.storage")
    store = LocalStore(_url(db))
    try:
        revision = await store.upgrade()
        await store.upgrade()
    finally:
        await store.close()
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 2
    assert revision in infos[0] and "new file libex.db" in infos[0]
    assert revision in infos[1] and "existing file libex.db" in infos[1]
    assert str(db.parent) not in caplog.text


# ------------------------------------------------------------
# Paths a URL must not smuggle in
# ------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "sqlite:///file:x.db",
    "sqlite+aiosqlite:///file:x.db",
    "sqlite:///file:/tmp/x.db",
])
def test_a_bare_file_uri_is_refused(url):
    with pytest.raises(StoreConfigError, match="file: URIs"):
        LocalStore(url)


async def test_the_same_store_has_foreign_keys_on_again_after_an_upgrade(db, monkeypatch):
    """The upgrade turns enforcement off on a connection the pool keeps, so
    the switch back is what stops the store's own later connections running
    unenforced; a fresh store's connections would hide a missing switch."""
    await _seed_linked_pair(db)
    _scratch_migration(monkeypatch)
    store = LocalStore(_url(db))
    try:
        await store.upgrade()
        await store.open()
        async with store.session() as session:
            assert (await session.execute(text("PRAGMA foreign_keys"))).scalar() == 1
    finally:
        await store.close()
