"""
Sessions over the same schema on SQLite and, when Docker is there, Postgres.
"""

# Third party
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

# Local
from libex_core.storage.base import Base
from libex_core.storage.store import LocalStore
from tests.libex_core.storage._support import core_tables, seed


@pytest_asyncio.fixture
async def sqlite_store():
    """An in-memory SQLite store, upgraded by the package's own migrations and
    opened, so the readers run against the engine setup and the schema a real
    store has."""
    store = LocalStore("sqlite+aiosqlite://")
    await store.upgrade()
    await store.open()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def sqlite_session(sqlite_store):
    async with sqlite_store.write() as session:
        await seed(session)
    async with sqlite_store.session() as session:
        yield session


@pytest.fixture(scope="module")
def postgres_url():
    docker = pytest.importorskip("docker")
    containers = pytest.importorskip("testcontainers.postgres")
    try:
        docker.from_env().ping()
    except Exception:
        pytest.skip("Docker daemon not available")
    with containers.PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql+asyncpg://")


@pytest_asyncio.fixture
async def postgres_session(postgres_url):
    engine = create_async_engine(postgres_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=core_tables())
        await conn.run_sync(Base.metadata.create_all, tables=core_tables())
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await seed(session)
        yield session
    await engine.dispose()
