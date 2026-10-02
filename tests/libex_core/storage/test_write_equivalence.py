"""
The same writes, run on real Postgres and on SQLite, must leave the same rows.

Each phase is written to both databases and the stored state compared table by
table, column by column, free only of what two databases cannot agree on
(timestamps and generated ids). The focused cases of `_write_cases` also run on
Postgres, where each states the row it expects, so two databases agreeing on a
wrong answer would still fail.
"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from libex_core.storage.dialect import configure_sqlite
from tests.libex_core.storage import _write_cases as cases

pytestmark = pytest.mark.integration

docker = pytest.importorskip("docker")
postgres_container = pytest.importorskip("testcontainers.postgres")


def _docker_available() -> bool:
    try:
        docker.from_env().ping()
        return True
    except Exception:
        return False


if not _docker_available():
    pytest.skip("Docker daemon not available", allow_module_level=True)


@pytest.fixture(scope="module")
def postgres_url():
    container = postgres_container.PostgresContainer("postgres:16")
    container.start()
    try:
        url = container.get_connection_url().replace("postgresql+psycopg2://", "postgresql+asyncpg://")

        async def prepare():
            engine = create_async_engine(url, poolclass=NullPool)
            async with engine.begin() as connection:
                await connection.run_sync(lambda c: cases.Base.metadata.create_all(c, tables=cases.core_tables()))
            await engine.dispose()

        asyncio.run(prepare())
        yield url
    finally:
        container.stop()


@pytest.fixture
async def pg_factory(postgres_url):
    engine = create_async_engine(postgres_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        names = ", ".join(cases.TABLES)
        await connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    await engine.dispose()


@pytest.fixture
async def lite_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'libex.db'}")
    configure_sqlite(engine)
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: cases.Base.metadata.create_all(c, tables=cases.core_tables()))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _assert_same(on_pg: dict, on_lite: dict, label: str) -> None:
    assert set(on_pg) == set(on_lite), label
    for key in on_pg:
        assert on_pg[key] == on_lite[key], (label, key)


@pytest.mark.parametrize("case", cases.FOCUSED, ids=lambda c: c.__name__)
async def test_focused_case_on_postgres(pg_factory, case):
    await case(pg_factory)


@pytest.mark.parametrize("case", cases.FOCUSED, ids=lambda c: c.__name__)
async def test_focused_case_leaves_the_same_rows_on_both(pg_factory, lite_factory, case):
    await case(pg_factory)
    await case(lite_factory)
    _assert_same(await cases.dump(pg_factory), await cases.dump(lite_factory), case.__name__)


async def test_the_corpus_leaves_the_same_rows_after_every_phase(pg_factory, lite_factory):
    on_pg = await cases.corpus_phases(pg_factory)
    on_lite = await cases.corpus_phases(lite_factory)
    assert set(on_pg) == set(on_lite) == {"rich", "thin", "as_sent", "richer"}
    for phase in on_pg:
        _assert_same(on_pg[phase], on_lite[phase], phase)
    cases.assert_nothing_shrank(on_pg["rich"], on_pg["thin"])


async def test_chapters_leave_the_same_rows_after_every_phase(pg_factory, lite_factory):
    on_pg = await cases.track_phases(pg_factory)
    on_lite = await cases.track_phases(lite_factory)
    assert set(on_pg) == set(on_lite)
    for phase in on_pg:
        if phase.endswith("_count"):
            assert on_pg[phase] == on_lite[phase], phase
        else:
            _assert_same(on_pg[phase], on_lite[phase], phase)


async def test_profiles_leave_the_same_rows_after_every_phase(pg_factory, lite_factory):
    on_pg = await cases.profile_phases(pg_factory)
    on_lite = await cases.profile_phases(lite_factory)
    for phase in on_pg:
        _assert_same(on_pg[phase], on_lite[phase], phase)
