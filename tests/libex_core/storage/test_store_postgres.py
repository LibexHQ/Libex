"""
`LocalStore` on a real Postgres, and the proof that the package's own
migrations build the schema the hosted app's chain builds.

The comparison reads both databases back through the catalog, so it is a
statement about what Postgres holds, not about what either set of migrations
says. Everything here needs Docker, through the container fixture in conftest.
"""

# Standard library
import asyncio
import os
import subprocess
import sys
import traceback
import uuid
from pathlib import Path

# Third party
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Local
from libex_core.storage.models import CORE_TABLES
from libex_core.storage.read import books
from libex_core.storage.store import (
    ForeignDatabase,
    LocalStore,
    StoreConnectionError,
    StoreNotInitialised,
)
from libex_core.storage.upgrade import CURRENT, VERSION_TABLE
from libex_core.storage.write import write_books
from tests.libex_core.storage import _write_cases as cases

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET = "pg-s3cret-pw"


async def _create_database(base_url: str) -> str:
    """A new empty database in the container; returns its URL."""
    name = "t_" + uuid.uuid4().hex[:12]
    engine = create_async_engine(base_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()
    return make_url(base_url).set(database=name).render_as_string(hide_password=False)


@pytest.fixture
async def url(postgres_url):
    return await _create_database(postgres_url)


async def test_upgrade_on_a_fresh_postgres_database_then_open_and_round_trip(url):
    store = LocalStore(url)
    try:
        assert (await store.status()).state == "empty"
        revision = await store.upgrade()
        state = await store.status()
        assert (state.state, state.revision) == (CURRENT, revision)
    finally:
        await store.close()

    async with LocalStore(url) as store:
        async with store.write() as session:
            await write_books(session, [cases.mk("B0PGROUND1", title="Round", description="d")])
        async with store.session() as session:
            book = await books.get_book(session, "B0PGROUND1")
    assert book["title"] == "Round"


async def test_a_second_upgrade_is_a_no_op(url):
    store = LocalStore(url)
    try:
        first = await store.upgrade()
        second = await store.upgrade()
        async with store._engine.connect() as connection:
            rows = (await connection.execute(text(f"SELECT version_num FROM {VERSION_TABLE}"))).all()
    finally:
        await store.close()
    assert first == second and rows == [(first,)]


async def test_two_stores_upgrading_one_database_at_once_both_succeed(url):
    stores = [LocalStore(url) for _ in range(3)]
    try:
        revisions = await asyncio.gather(*(s.upgrade() for s in stores))
    finally:
        for s in stores:
            await s.close()
    assert len(set(revisions)) == 1


async def test_open_before_upgrade_refuses_and_creates_nothing(url):
    store = LocalStore(url)
    try:
        with pytest.raises(StoreNotInitialised):
            await store.open()
        async with store._engine.connect() as connection:
            tables = await connection.run_sync(lambda c: inspect(c).get_table_names())
    finally:
        await store.close()
    assert tables == []


async def test_a_database_with_tables_and_no_core_revision_is_refused_and_untouched(url):
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE alembic_version (version_num varchar(32) NOT NULL)"))
        await connection.execute(text("INSERT INTO alembic_version VALUES ('fd5fff5ee0e3')"))
        await connection.execute(text("CREATE TABLE books (asin varchar(12) PRIMARY KEY)"))
    for call in ("open", "upgrade"):
        store = LocalStore(url)
        try:
            with pytest.raises(ForeignDatabase):
                await getattr(store, call)()
        finally:
            await store.close()
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda c: sorted(inspect(c).get_table_names()))
        columns = await connection.run_sync(lambda c: [x["name"] for x in inspect(c).get_columns("books")])
    await engine.dispose()
    assert tables == ["alembic_version", "books"]
    assert columns == ["asin"]


async def test_a_failed_upgrade_leaves_nothing_behind(url, monkeypatch):
    import libex_core.storage.store as store_module

    def boom(connection):
        raise RuntimeError("after the DDL")

    monkeypatch.setattr(store_module, "head_revision", boom)
    store = LocalStore(url)
    try:
        with pytest.raises(RuntimeError, match="after the DDL"):
            await store.upgrade()
        async with store._engine.connect() as connection:
            tables = await connection.run_sync(lambda c: inspect(c).get_table_names())
            enums = (await connection.execute(text("SELECT count(*) FROM pg_type WHERE typtype = 'e'"))).scalar()
    finally:
        await store.close()
    assert tables == [] and enums == 0


async def test_a_bad_password_reports_the_class_and_never_the_password(postgres_url):
    parsed = make_url(postgres_url)
    bad = parsed.set(password=SECRET).render_as_string(hide_password=False)
    store = LocalStore(bad)
    try:
        for call in (store.status, store.upgrade, store.open):
            with pytest.raises(StoreConnectionError) as caught:
                await call()
            exc = caught.value
            assert SECRET not in "".join(traceback.format_exception(exc))
            assert exc.__cause__ is None
    finally:
        await store.close()
    assert SECRET not in repr(store)


async def test_a_session_failure_to_connect_does_not_carry_the_password_either(postgres_url):
    parsed = make_url(postgres_url)
    bad = parsed.set(password=SECRET).render_as_string(hide_password=False)
    # Skip the schema check by opening the store by hand; the first query is
    # the first connection, which is where a driver error would surface.
    store = LocalStore(bad)
    store._opened = True
    try:
        with pytest.raises(Exception) as caught:
            async with store.session() as session:
                await session.execute(text("SELECT 1"))
    finally:
        await store.close()
    exc, seen = caught.value, []
    while exc is not None and exc not in seen:
        seen.append(exc)
        assert SECRET not in str(exc) and SECRET not in repr(exc)
        exc = exc.__cause__ or exc.__context__
    assert SECRET not in "".join(traceback.format_exception(caught.value))


# ------------------------------------------------------------
# The same schema as the hosted chain
# ------------------------------------------------------------

async def _snapshot(url: str) -> dict:
    """Everything Postgres holds about the core tables, in a form that does not
    depend on the order columns were added in."""
    engine = create_async_engine(url, poolclass=NullPool)

    def read(connection):
        i = inspect(connection)
        out = {}
        for table in sorted(CORE_TABLES):
            pk = i.get_pk_constraint(table)
            out[table] = {
                "columns": sorted(
                    (c["name"], str(c["type"]), c["nullable"], str(c["default"]))
                    for c in i.get_columns(table)
                ),
                "pk": (pk["name"], pk["constrained_columns"]),
                "unique": sorted((u["name"], tuple(u["column_names"])) for u in i.get_unique_constraints(table)),
                "indexes": sorted(
                    (x["name"], tuple(x["column_names"]), x["unique"], str(x.get("dialect_options")))
                    for x in i.get_indexes(table)
                ),
                "foreign_keys": sorted(
                    (f["name"], tuple(f["constrained_columns"]), f["referred_table"],
                     tuple(f["referred_columns"]), str(f.get("options")))
                    for f in i.get_foreign_keys(table)
                ),
                "checks": sorted(str(c) for c in i.get_check_constraints(table)),
            }
        return out

    try:
        async with engine.connect() as connection:
            snap = await connection.run_sync(read)
            snap["enums"] = sorted(
                tuple(r) for r in await connection.execute(text(
                    "SELECT t.typname, string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) "
                    "FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid GROUP BY 1"))
            )
            snap["column_order_free_identity"] = sorted(
                tuple(r) for r in await connection.execute(text(
                    "SELECT table_name, column_name, is_identity, identity_generation "
                    "FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND table_name = ANY(:names)"), {"names": sorted(CORE_TABLES)})
            )
    finally:
        await engine.dispose()
    return snap


def _migrate_hosted(url: str) -> None:
    """The hosted app's own chain, run exactly as the deployment runs it."""
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]


async def test_the_core_chain_builds_the_schema_the_hosted_chain_builds(postgres_url):
    hosted_url = await _create_database(postgres_url)
    core_url = await _create_database(postgres_url)
    await asyncio.to_thread(_migrate_hosted, hosted_url)
    store = LocalStore(core_url)
    try:
        await store.upgrade()
    finally:
        await store.close()

    hosted, core = await _snapshot(hosted_url), await _snapshot(core_url)

    assert core.keys() == hosted.keys()
    for key in hosted:
        assert core[key] == hosted[key], key
    # Not vacuous: the comparison saw real content on both sides.
    assert len(core) > len(CORE_TABLES) and core["books"]["indexes"] and core["enums"]


async def test_the_core_chain_adds_no_table_beyond_the_core_ones_and_its_own_version_table(url):
    store = LocalStore(url)
    try:
        await store.upgrade()
        async with store._engine.connect() as connection:
            tables = set(await connection.run_sync(lambda c: inspect(c).get_table_names()))
    finally:
        await store.close()
    assert tables == set(CORE_TABLES) | {VERSION_TABLE}


async def test_the_migrated_schema_matches_the_models_on_postgres(url):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import libex_core.storage.models  # noqa: F401
    from libex_core.storage.base import Base

    def diff(connection):
        context = MigrationContext.configure(connection, opts={
            "compare_type": True,
            "include_object": lambda o, n, t, r, c: t != "table" or n in CORE_TABLES,
        })
        return compare_metadata(context, Base.metadata)

    store = LocalStore(url)
    try:
        await store.upgrade()
        async with store._engine.connect() as connection:
            assert await connection.run_sync(diff) == []
    finally:
        await store.close()


async def test_the_initial_revision_reverses_on_postgres_including_its_enum_types(url):
    from alembic import command

    from libex_core.storage.upgrade import _config

    store = LocalStore(url)
    try:
        await store.upgrade()
        async with store._engine.begin() as connection:
            await connection.run_sync(lambda c: command.downgrade(_config(c), "base"))
        async with store._engine.connect() as connection:
            tables = await connection.run_sync(lambda c: inspect(c).get_table_names())
            enums = (await connection.execute(text("SELECT count(*) FROM pg_type WHERE typtype = 'e'"))).scalar()
    finally:
        await store.close()
    assert tables == [VERSION_TABLE] and enums == 0
