"""
Sessions over the same schema on SQLite and, when Docker is there, Postgres.
"""

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

# Local
from libex_core.storage.base import Base
from tests.libex_core.storage._support import core_tables, seed


def _simple_lower(value):
    """Lower-cases one code point at a time and keeps any that would expand, as
    Postgres' lower() does: no multi-character results, no final-sigma rule."""
    if value is None:
        return None
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in value)


@pytest_asyncio.fixture
async def sqlite_engine():
    """In-memory SQLite with a Unicode-aware lower(), the one function the
    connection setup owes the readers: SQLite's own lowers ASCII only."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _register(dbapi_connection, _record):
        dbapi_connection.create_function("lower", 1, _simple_lower, deterministic=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=core_tables())
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def sqlite_session(sqlite_engine):
    async with async_sessionmaker(sqlite_engine, expire_on_commit=False)() as session:
        await seed(session)
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
