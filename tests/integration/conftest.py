"""
Integration test harness.
Spins up a real PostgreSQL 16 container, runs Alembic migrations against it,
and provides a real AsyncSession bound to it.

These tests exercise actual Postgres behaviour (sorting, casts, regex) that
mocked sessions cannot cover. They require Docker; when Docker is absent the
whole module is skipped rather than failing, so the rest of the suite runs.

IMPORTANT: the container must start and DATABASE_URL must be set BEFORE any
app module (which caches settings) is imported. So the container is started at
module import time, not inside a fixture, and the app/Alembic imports happen
only after the env var is in place.
"""

# Standard library
import os
from collections.abc import AsyncGenerator

# Third party
import pytest
import pytest_asyncio

# Skip cleanly if Docker tooling/daemon isn't available.
docker = pytest.importorskip("docker")
testcontainers_postgres = pytest.importorskip("testcontainers.postgres")

from testcontainers.postgres import PostgresContainer  # noqa: E402


def _docker_available() -> bool:
    try:
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


if not _docker_available():
    pytest.skip(
        "Docker daemon not available — skipping integration tests",
        allow_module_level=True,
    )

# --- Start the container at import time, before any app import. ---
_POSTGRES = PostgresContainer("postgres:16")
_POSTGRES.start()

_raw_url = _POSTGRES.get_connection_url()
_ASYNC_URL = _raw_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")

# Set the env var BEFORE importing anything that reads settings.
os.environ["DATABASE_URL"] = _ASYNC_URL

# Now it is safe to import app modules — they will read the container URL.
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core import config  # noqa: E402

# Clear any cached settings so the new env var is authoritative.
config.get_settings.cache_clear()

# Run migrations to head against the container using the real Alembic chain.
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

_alembic_cfg = Config("alembic.ini")
_alembic_cfg.set_main_option("sqlalchemy.url", _ASYNC_URL)
command.upgrade(_alembic_cfg, "head")


def pytest_unconfigure(config):
    """Stop the container when the test session ends."""
    try:
        _POSTGRES.stop()
    except Exception:
        pass


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provides a real AsyncSession bound to the migrated container DB."""
    engine = create_async_engine(_ASYNC_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()