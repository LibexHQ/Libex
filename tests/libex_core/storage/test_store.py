"""
`LocalStore` on SQLite: the lifecycle, the refusals, and the promise that the
connection string never leaves the object that was given it.

The Postgres half, which needs a container, is in test_store_postgres.py.
"""

# Standard library
import asyncio
import logging
import os
import sqlite3
import stat
import subprocess
import sys
import traceback
from pathlib import Path

# Third party
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

# Local
from libex_core.storage import StorageUnavailable, upgrade as upgrade_module
from libex_core.storage.read import books
from libex_core.storage.store import (
    ForeignDatabase,
    LocalStore,
    StoreClosed,
    StoreConfigError,
    StoreConnectionError,
    StoreError,
    StoreNotInitialised,
    StoreOutdated,
)
from libex_core.storage.upgrade import (
    AHEAD,
    BEHIND,
    CURRENT,
    EMPTY,
    FOREIGN,
    VERSION_TABLE,
    SchemaState,
)
from libex_core.storage.write import write_books
from tests.libex_core.storage import _write_cases as cases

REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET = "hunter2-s3cret"
POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")


def _url(path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _tables(path) -> set[str]:
    with sqlite3.connect(path) as db:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


@pytest.fixture
def db(tmp_path) -> Path:
    return tmp_path / "libex.db"


@pytest.fixture
async def upgraded(db):
    store = LocalStore(_url(db))
    await store.upgrade()
    await store.close()
    return db


# ------------------------------------------------------------
# Upgrade, status, open
# ------------------------------------------------------------

async def test_upgrade_on_a_fresh_file_creates_the_schema(db):
    store = LocalStore(_url(db))
    try:
        assert not db.exists()
        assert (await store.status()) == SchemaState(EMPTY, None)
        assert not db.exists(), "reading the status must not create the file"

        revision = await store.upgrade()

        state = await store.status()
        assert state == SchemaState(CURRENT, revision)
        tables = _tables(db)
        assert VERSION_TABLE in tables
        assert {"books", "authors", "series", "narrators", "genres", "tracks"} <= tables
    finally:
        await store.close()


async def test_the_head_is_the_one_revision_the_package_ships(upgraded):
    versions = REPO_ROOT / "libex_core" / "storage" / "migrations" / "versions"
    names = sorted(p.name for p in versions.glob("*.py"))
    assert len(names) == 1
    store = LocalStore(_url(upgraded))
    try:
        state = await store.status()
    finally:
        await store.close()
    assert names[0].startswith(state.revision + "_")
    assert len(state.revision) == 12 and int(state.revision, 16) >= 0


async def test_a_second_upgrade_changes_nothing(upgraded):
    before = sqlite3.connect(upgraded).execute(f"SELECT * FROM {VERSION_TABLE}").fetchall()
    store = LocalStore(_url(upgraded))
    try:
        again = await store.upgrade()
    finally:
        await store.close()
    after = sqlite3.connect(upgraded).execute(f"SELECT * FROM {VERSION_TABLE}").fetchall()
    assert before == after == [(again,)]


async def test_two_stores_upgrading_one_new_file_both_succeed(db):
    stores = [LocalStore(_url(db)) for _ in range(2)]
    try:
        revisions = await asyncio.gather(*(s.upgrade() for s in stores))
    finally:
        for s in stores:
            await s.close()
    assert revisions[0] == revisions[1]


async def test_open_before_upgrade_refuses_and_creates_nothing(db):
    with pytest.raises(StoreNotInitialised, match="upgrade"):
        await LocalStore(_url(db)).open()
    assert not db.exists()


async def test_open_on_an_empty_existing_file_refuses_without_changing_it(db):
    db.write_bytes(b"")
    with pytest.raises(StoreNotInitialised):
        await LocalStore(_url(db)).open()
    assert db.read_bytes() == b""


async def test_open_never_upgrades(db):
    async def attempt():
        async with LocalStore(_url(db)):
            pass

    with pytest.raises(StoreNotInitialised):
        await attempt()
    assert not db.exists()


async def test_open_succeeds_on_an_upgraded_database(upgraded):
    async with LocalStore(_url(upgraded)) as store:
        async with store.session() as session:
            assert (await session.execute(text("SELECT count(*) FROM books"))).scalar() == 0


# ------------------------------------------------------------
# Someone else's database, and one from the future
# ------------------------------------------------------------

@pytest.fixture
def foreign(db) -> Path:
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO notes (body) VALUES ('mine')")
    return db


async def test_a_foreign_database_is_refused_by_open_and_upgrade_and_left_alone(foreign):
    for call in ("open", "upgrade"):
        store = LocalStore(_url(foreign))
        try:
            with pytest.raises(ForeignDatabase):
                await getattr(store, call)()
        finally:
            await store.close()
    assert _tables(foreign) == {"notes"}
    assert sqlite3.connect(foreign).execute("SELECT body FROM notes").fetchall() == [("mine",)]


async def test_refusing_a_foreign_database_does_not_change_how_it_journals(foreign):
    for call in ("status", "open", "upgrade"):
        store = LocalStore(_url(foreign))
        try:
            try:
                await getattr(store, call)()
            except ForeignDatabase:
                pass
        finally:
            await store.close()
    mode = sqlite3.connect(foreign).execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "delete"


async def test_a_database_with_the_hosted_version_table_is_foreign(db):
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        conn.execute("INSERT INTO alembic_version VALUES ('fd5fff5ee0e3')")
        conn.execute("CREATE TABLE books (asin TEXT PRIMARY KEY)")
    store = LocalStore(_url(db))
    try:
        with pytest.raises(ForeignDatabase):
            await store.upgrade()
    finally:
        await store.close()
    assert _tables(db) == {"alembic_version", "books"}


async def test_a_revision_this_library_has_never_heard_of_is_refused(upgraded):
    with sqlite3.connect(upgraded) as conn:
        conn.execute(f"UPDATE {VERSION_TABLE} SET version_num = 'ffffffffffff'")
    assert (await _status(upgraded)).state == AHEAD
    for call in ("open", "upgrade"):
        store = LocalStore(_url(upgraded))
        try:
            with pytest.raises(StoreOutdated, match="newer"):
                await getattr(store, call)()
        finally:
            await store.close()


async def _status(path):
    store = LocalStore(_url(path))
    try:
        return await store.status()
    finally:
        await store.close()


async def test_an_older_known_revision_is_behind_and_open_says_to_upgrade(upgraded, monkeypatch):
    """The chain has one revision today, so a stored older one is stood in for:
    the script directory is told the stored revision is known and not the head."""

    class _Script:
        def get_current_head(self):
            return "0123456789ab"

        def get_revision(self, revision):
            return object()

    monkeypatch.setattr(upgrade_module, "_script", lambda connection: _Script())
    store = LocalStore(_url(upgraded))
    try:
        assert (await store.status()).state == BEHIND
        with pytest.raises(StoreOutdated, match="upgrade"):
            await store.open()
    finally:
        await store.close()


async def test_the_state_of_an_unrecorded_database_is_foreign_not_empty(foreign):
    assert (await _status(foreign)).state == FOREIGN


async def test_a_failed_upgrade_leaves_no_tables_behind(db, monkeypatch):
    """The migration runs inside the transaction the store opens, so a failure
    after it has created everything still leaves the file as it was."""

    def boom(connection):
        raise RuntimeError("after the DDL")

    import libex_core.storage.store as store_module

    monkeypatch.setattr(store_module, "head_revision", boom)
    store = LocalStore(_url(db))
    try:
        with pytest.raises(RuntimeError, match="after the DDL"):
            await store.upgrade()
    finally:
        await store.close()
    assert _tables(db) == set()


def test_no_downgrade_is_offered():
    assert not hasattr(LocalStore, "downgrade")
    assert not any("downgrade" in name for name in dir(LocalStore))


async def test_the_initial_revision_does_reverse_when_run_by_hand(upgraded):
    """Not something the store does; the chain is reversible all the same."""
    from alembic import command

    from libex_core.storage.upgrade import _config

    conn = sqlite3.connect(upgraded)
    conn.close()
    store = LocalStore(_url(upgraded))
    try:
        async with store._engine.connect() as connection:
            await connection.run_sync(lambda c: command.downgrade(_config(c), "base"))
            await connection.commit()
    finally:
        await store.close()
    assert _tables(upgraded) <= {VERSION_TABLE, "sqlite_sequence"}


# ------------------------------------------------------------
# The schema SQLite gets
# ------------------------------------------------------------

async def test_the_migrated_schema_matches_the_models(upgraded):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import libex_core.storage.models  # noqa: F401
    from libex_core.storage.base import Base
    from libex_core.storage.models import CORE_TABLES

    def diff(connection):
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "include_object": lambda o, n, t, r, c: t != "table" or n in CORE_TABLES,
            },
        )
        return compare_metadata(context, Base.metadata)

    store = LocalStore(_url(upgraded))
    try:
        async with store._engine.connect() as connection:
            assert await connection.run_sync(diff) == []
    finally:
        await store.close()


async def test_a_value_outside_an_enum_is_refused_on_sqlite(upgraded):
    store = LocalStore(_url(upgraded))
    try:
        await store.open()
        book = "INSERT INTO books (asin, title, region, explicit, whisper_sync, has_pdf, is_listenable, is_buyable, is_vvab, created_at, updated_at) VALUES ('B0CHECK001', 't', :region, 0, 0, 0, 1, 1, 0, '2026-01-01', '2026-01-01')"
        async with store.write() as session:
            await session.execute(text(book), {"region": "us"})
        for bad in ("xx", "US", ""):
            with pytest.raises(IntegrityError):
                async with store.write() as session:
                    await session.execute(text(book.replace("B0CHECK001", "B0CHECK002")), {"region": bad})
        with pytest.raises(IntegrityError):
            async with store.write() as session:
                await session.execute(text(
                    "INSERT INTO authors (name, region, fetched_description, created_at, updated_at) "
                    "VALUES ('a', 'zz', 0, '2026-01-01', '2026-01-01')"))
        with pytest.raises(IntegrityError):
            async with store.write() as session:
                await session.execute(text(
                    "INSERT INTO genres (asin, name, type, created_at, updated_at) "
                    "VALUES ('G000000001', 'g', 'Other', '2026-01-01', '2026-01-01')"))
        async with store.write() as session:  # a nullable enum column takes NULL
            await session.execute(text(
                "INSERT INTO series (asin, title, region, fetched_description, created_at, updated_at) "
                "VALUES ('S000000001', 's', NULL, 0, '2026-01-01', '2026-01-01')"))
            await session.execute(text(
                "INSERT INTO genres (asin, name, type, created_at, updated_at) "
                "VALUES ('G000000002', 'g', 'Tags', '2026-01-01', '2026-01-01')"))
        async with store.session() as session:
            assert (await session.execute(text("SELECT count(*) FROM books"))).scalar() == 1
    finally:
        await store.close()


async def test_every_region_the_models_allow_is_accepted(upgraded):
    from libex_core.storage.models import REGION_ENUM

    store = LocalStore(_url(upgraded))
    try:
        await store.open()
        async with store.write() as session:
            for index, region in enumerate(REGION_ENUM.enums):
                await session.execute(text(
                    "INSERT INTO series (asin, title, region, fetched_description, created_at, updated_at) "
                    f"VALUES ('S0000000{index:02d}', 's', '{region}', 0, '2026-01-01', '2026-01-01')"))
    finally:
        await store.close()


# ------------------------------------------------------------
# Writing and reading through the store
# ------------------------------------------------------------

async def test_a_write_through_the_store_reads_back_through_the_store(upgraded):
    async with LocalStore(_url(upgraded)) as store:
        async with store.write() as session:
            await write_books(session, [cases.mk(
                "B0ROUNDTRP", title="Round Trip", description="kept",
                authors=[{"asin": "A000000001", "name": "Author One", "region": "us"}],
            )])
        async with store.session() as session:
            book = await books.get_book(session, "B0ROUNDTRP")
    assert book["title"] == "Round Trip"
    assert book["description"] == "kept"
    assert [a["name"] for a in book["authors"]] == ["Author One"]


async def test_a_second_store_on_the_same_file_sees_the_committed_write(upgraded):
    async with LocalStore(_url(upgraded)) as first:
        async with first.write() as session:
            await write_books(session, [cases.mk("B0SHARED002")])
        async with LocalStore(_url(upgraded)) as second:
            async with second.session() as session:
                assert await books.get_book(session, "B0SHARED002") is not None


async def test_a_block_that_raises_rolls_the_write_back(upgraded):
    async with LocalStore(_url(upgraded)) as store:
        with pytest.raises(RuntimeError, match="stop"):
            async with store.write() as session:
                await write_books(session, [cases.mk("B0ROLLBK001")])
                raise RuntimeError("stop")
        async with store.session() as session:
            assert await books.get_book(session, "B0ROLLBK001") is None


async def test_a_read_session_commits_nothing(upgraded):
    async with LocalStore(_url(upgraded)) as store:
        async with store.session() as session:
            await write_books(session, [cases.mk("B0NOCOMMIT1")])
        async with store.session() as session:
            assert await books.get_book(session, "B0NOCOMMIT1") is None


async def test_writes_through_one_store_queue_rather_than_collide(upgraded):
    async with LocalStore(_url(upgraded)) as store:
        async def one(n):
            async with store.write() as session:
                await write_books(session, [cases.mk(f"B0QUEUE{n:04d}")])

        await asyncio.gather(*(one(n) for n in range(20)))
        async with store.session() as session:
            count = (await session.execute(text("SELECT count(*) FROM books"))).scalar()
    assert count == 20


async def test_the_sqlite_setup_is_applied_to_the_stores_connections(upgraded):
    async with LocalStore(_url(upgraded)) as store:
        async with store.session() as session:
            assert (await session.execute(text("PRAGMA foreign_keys"))).scalar() == 1
            assert (await session.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
            assert (await session.execute(text("SELECT lower('ÉMILE')"))).scalar() == "émile"


# ------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------

async def test_sessions_need_an_open_store(upgraded):
    store = LocalStore(_url(upgraded))
    try:
        for factory in (store.session, store.write):
            with pytest.raises(StoreClosed):
                async with factory():
                    pass
        await store.open()
        async with store.session():
            pass
    finally:
        await store.close()
    for factory in (store.session, store.write):
        with pytest.raises(StoreClosed):
            async with factory():
                pass
    with pytest.raises(StoreClosed):
        await store.upgrade()


async def test_close_is_safe_twice_and_on_a_store_never_opened(db):
    store = LocalStore(_url(db))
    await store.close()
    await store.close()


async def test_async_with_closes_even_when_the_block_raises(upgraded):
    with pytest.raises(RuntimeError, match="inside"):
        async with LocalStore(_url(upgraded)) as store:
            raise RuntimeError("inside")
    with pytest.raises(StoreClosed):
        async with store.session():
            pass


async def test_a_refused_open_closes_the_engine(foreign):
    store = LocalStore(_url(foreign))
    with pytest.raises(ForeignDatabase):
        async with store:
            pass
    with pytest.raises(StoreClosed):
        await store.upgrade()


async def test_an_in_memory_store_works_end_to_end():
    memory = LocalStore("sqlite+aiosqlite://")
    try:
        await memory.upgrade()
        await memory.open()
        async with memory.write() as session:
            await write_books(session, [cases.mk("B0MEMORY001")])
        async with memory.session() as session:
            assert await books.get_book(session, "B0MEMORY001") is not None
    finally:
        await memory.close()


# ------------------------------------------------------------
# The file
# ------------------------------------------------------------

@POSIX
async def test_a_new_file_and_its_new_directory_are_private(tmp_path):
    path = tmp_path / "nested" / "deeper" / "libex.db"
    old = os.umask(0o022)
    try:
        store = LocalStore(_url(path))
        try:
            await store.upgrade()
            assert os.umask(0o022) == 0o022, "the umask must be left alone"
        finally:
            await store.close()
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    for sidecar in path.parent.glob("libex.db-*"):
        assert stat.S_IMODE(sidecar.stat().st_mode) & 0o077 == 0


@POSIX
async def test_a_permissive_umask_cannot_loosen_the_file(tmp_path):
    path = tmp_path / "libex.db"
    old = os.umask(0)
    try:
        store = LocalStore(_url(path))
        try:
            await store.upgrade()
        finally:
            await store.close()
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@POSIX
async def test_an_existing_loose_file_is_warned_about_and_not_changed(upgraded, caplog):
    upgraded.chmod(0o644)
    caplog.set_level(logging.WARNING, logger="libex")
    async with LocalStore(_url(upgraded)):
        pass
    assert any("accessible to other users" in r.getMessage() for r in caplog.records)
    assert upgraded.name in caplog.text
    assert str(upgraded.parent) not in caplog.text
    assert stat.S_IMODE(upgraded.stat().st_mode) == 0o644


@POSIX
async def test_a_private_existing_file_draws_no_warning(upgraded, caplog):
    upgraded.chmod(0o600)
    caplog.set_level(logging.WARNING, logger="libex")
    async with LocalStore(_url(upgraded)):
        pass
    assert caplog.records == []


@POSIX
async def test_a_symlink_is_refused_not_followed(tmp_path):
    target = tmp_path / "elsewhere.db"
    link = tmp_path / "link.db"
    link.symlink_to(target)
    store = LocalStore(_url(link))
    try:
        with pytest.raises(StoreConfigError, match="symbolic link"):
            await store.upgrade()
        with pytest.raises(StoreConfigError, match="symbolic link"):
            await store.status()
    finally:
        await store.close()
    assert not target.exists()


async def test_a_directory_is_refused(tmp_path):
    store = LocalStore(_url(tmp_path))
    try:
        with pytest.raises(StoreConfigError, match="regular file"):
            await store.upgrade()
    finally:
        await store.close()


# ------------------------------------------------------------
# What a URL may be
# ------------------------------------------------------------

@pytest.mark.parametrize("url", [
    f"mysql+aiomysql://u:{SECRET}@h/d",
    f"postgresql+psycopg2://u:{SECRET}@h/d",
    f"postgresql+asyncpg://u:{SECRET}@h/d?plugin=evil",
    f"postgresql+asyncpg://u:{SECRET}@h/d?host=/tmp",
    f"postgresql+asyncpg://u:{SECRET}@h/d?ssl=require&ssl=prefer",
    f"postgresql+asyncpg://u:{SECRET}@h",
    f"postgresql+asyncpg://:{SECRET}@h/d",
    f"postgresql+asyncpg://u:{SECRET}@/d",
    f"sqlite+aiosqlite://u:{SECRET}@h/x.db",
    "sqlite+aiosqlite:///x.db?uri=true",
    "sqlite+aiosqlite:///file:x.db?mode=ro",
    "sqlite+aiosqlite:///x\x00.db",
    f"not a url with {SECRET}",
    "",
])
def test_an_unacceptable_url_is_refused_without_quoting_it(url):
    with pytest.raises(StoreConfigError) as caught:
        LocalStore(url)
    _assert_no_secret(caught.value)


def test_a_url_of_the_wrong_type_is_refused():
    with pytest.raises(StoreConfigError):
        LocalStore(12345)  # type: ignore[arg-type]


@pytest.mark.parametrize("url", [
    "sqlite:///x.db",
    "sqlite+aiosqlite:///x.db",
    "sqlite+aiosqlite://",
    f"postgresql://u:{SECRET}@h/d",
    f"postgresql+asyncpg://u:{SECRET}@h:5433/d?ssl=require&application_name=x",
])
def test_the_acceptable_forms_construct(url):
    store = LocalStore(url)
    assert store.backend in ("sqlite", "postgresql")
    _assert_no_secret(repr(store))


def test_postgres_defaults_the_port_so_no_environment_variable_fills_it():
    store = LocalStore(f"postgresql://u:{SECRET}@h/d")
    assert store._url.port == 5432
    assert store._url.drivername == "postgresql+asyncpg"


def test_postgres_without_asyncpg_names_the_extra(monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "asyncpg" else real(name, *a)
    )
    with pytest.raises(StorageUnavailable) as caught:
        LocalStore(f"postgresql://u:{SECRET}@h/d")
    assert "libex-core[postgres]" in str(caught.value)
    _assert_no_secret(caught.value)


# ------------------------------------------------------------
# The secret stays put
# ------------------------------------------------------------

def _assert_no_secret(thing) -> None:
    """Looks at every place a secret could surface: the text, the repr, the
    arguments, and the whole cause and context chain as it would print."""
    if isinstance(thing, BaseException):
        seen, stack = [], [thing]
        while stack:
            exc = stack.pop()
            if exc is None or exc in seen:
                continue
            seen.append(exc)
            stack += [exc.__cause__, exc.__context__]
        for exc in seen:
            assert SECRET not in str(exc) and SECRET not in repr(exc), type(exc)
            assert all(SECRET not in repr(a) for a in exc.args)
        rendered = "".join(traceback.format_exception(thing))
        assert SECRET not in rendered
    else:
        assert SECRET not in str(thing)


async def test_a_failed_connection_reports_only_the_error_class(caplog):
    caplog.set_level(logging.DEBUG)
    store = LocalStore(f"postgresql+asyncpg://user:{SECRET}@127.0.0.1:1/libex")
    try:
        for call in (store.status, store.upgrade, store.open):
            with pytest.raises(StoreConnectionError) as caught:
                await call()
            _assert_no_secret(caught.value)
            assert caught.value.__cause__ is None and caught.value.__suppress_context__
            assert "postgresql" in str(caught.value)
    finally:
        await store.close()
    assert SECRET not in caplog.text
    assert SECRET not in repr(store) and SECRET not in str(store)


async def test_the_engine_hides_statement_parameters(upgraded):
    async with LocalStore(_url(upgraded)) as store:
        assert store._engine.sync_engine.hide_parameters is True
        assert store._engine.echo is False


async def test_a_database_error_does_not_echo_a_bound_value(upgraded):
    async with LocalStore(_url(upgraded)) as store:
        with pytest.raises(IntegrityError) as caught:
            async with store.write() as session:
                await session.execute(
                    text("INSERT INTO books (asin) VALUES (:asin)"), {"asin": SECRET}
                )
    assert SECRET not in str(caught.value)


def test_the_store_error_family_has_one_root():
    for cls in (ForeignDatabase, StoreClosed, StoreConfigError, StoreConnectionError,
                StoreNotInitialised, StoreOutdated):
        assert issubclass(cls, StoreError)
    assert issubclass(StoreConfigError, ValueError)


# ------------------------------------------------------------
# Import hygiene
# ------------------------------------------------------------

def _run(code: str) -> str:
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True,
        cwd=REPO_ROOT, env={"PYTHONPATH": str(REPO_ROOT), **_site_env()},
    )
    return out.stdout.strip()


def _site_env() -> dict:
    return {"PATH": os.environ.get("PATH", "")}


def test_importing_the_store_loads_no_alembic_until_an_upgrade_or_a_status_read():
    code = (
        "import asyncio, sys, tempfile, os\n"
        "import libex_core.storage.store as s\n"
        "import libex_core.storage.upgrade\n"
        "a = 'alembic' in sys.modules\n"
        "store = s.LocalStore('sqlite+aiosqlite:///' + os.path.join(tempfile.mkdtemp(), 'x.db'))\n"
        "asyncio.run(store.status())\n"
        "b = 'alembic' in sys.modules\n"
        "async def go():\n"
        "    await store.upgrade()\n"
        "    await store.close()\n"
        "asyncio.run(go())\n"
        "print(a, b, 'alembic' in sys.modules)\n"
    )
    assert _run(code) == "False False True"


def test_the_package_import_still_loads_neither_the_store_nor_sqlalchemy():
    code = (
        "import sys\n"
        "import libex_core.storage\n"
        "print([m for m in ('sqlalchemy', 'alembic', 'aiosqlite', 'asyncpg', 'libex_core.storage.store') "
        "if m in sys.modules])\n"
    )
    assert _run(code) == "[]"


def test_the_core_tables_constant_is_exactly_what_the_models_define():
    code = (
        "import libex_core.storage.models as m\n"
        "from libex_core.storage.base import Base\n"
        "print(sorted(Base.metadata.tables) == sorted(m.CORE_TABLES))\n"
    )
    assert _run(code) == "True"
