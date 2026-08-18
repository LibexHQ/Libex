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
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core import config  # noqa: E402

# Clear any cached settings so the new env var is authoritative.
config.get_settings.cache_clear()

# Run migrations to head against the container using the real Alembic chain.
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

_alembic_cfg = Config("alembic.ini")
_alembic_cfg.set_main_option("sqlalchemy.url", _ASYNC_URL)
command.upgrade(_alembic_cfg, "head")

# --- Rebind the app's own module-level engine to the container too. ---
#
# app.db.session.engine is created once, at that module's first import, from
# whatever DATABASE_URL settings held at that moment. The root tests/conftest.py
# imports app.main (and so app.db.session) before this file ever runs, which
# freezes that engine against the real .env database rather than the container
# started above -- setting the env var here is too late for anything that
# already captured the old engine by value, which is exactly what happens to
# every `from app.db.session import engine` import, persist_queue's included.
# Rebuilding the engine and reassigning it in place on both modules is what
# makes a background persist path built on that singleton (rather than a
# fixture-owned session) reachable by these tests at all: the alternative is
# mocking the very call the background path makes to get a session, which
# would prove nothing about it running against a real database.
import app.db.session as _db_session_module  # noqa: E402
import app.services.db.persist_queue as _persist_queue_module  # noqa: E402

# NullPool rather than the default pooled engine: pytest-asyncio hands each
# test function its own event loop, and a pooled asyncpg connection checked
# back in under one loop and checked out again under a later one raises or
# warns at teardown rather than being reused cleanly. Background persist calls
# open one connection and release it immediately either way, so pooling would
# only be buying reuse across a boundary that discards it regardless.
#
# hide_parameters=True is carried over deliberately, not dropped as a test
# convenience: this replaces the process's own app.db.session.engine, and the
# very first version of this rebind left it off, which silently turned off
# parameter-hiding for the rest of the pytest process rather than just for
# this module -- tests/services/test_db_error_logging.py reads
# app.db.session.engine directly to assert hide_parameters is set, and failed
# downstream of this file for exactly that reason: not pre-existing flakiness,
# a real regression this rebind introduced. Matched here to app/db/session.py's
# own construction rather than to whatever that module happens to say today,
# because a real DBAPI failure against the container must hide its bound
# values the same way one against production would, or this harness would be
# the one place in the whole suite where a leak could hide undetected.
_container_engine = create_async_engine(
    _ASYNC_URL,
    hide_parameters=True,
    pool_pre_ping=True,
    poolclass=NullPool,
    connect_args={
        "server_settings": {
            "statement_timeout": "30000",
            "idle_in_transaction_session_timeout": "60000",
        }
    },
)
_db_session_module.engine = _container_engine
_db_session_module.AsyncSessionFactory = async_sessionmaker(
    _container_engine, class_=AsyncSession, expire_on_commit=False,
)
_persist_queue_module._BackgroundSession = async_sessionmaker(
    _container_engine, expire_on_commit=False,
)


def pytest_unconfigure(config):
    """Stop the container when the test session ends."""
    try:
        import asyncio
        asyncio.run(_container_engine.dispose())
    except Exception:
        pass
    try:
        _POSTGRES.stop()
    except Exception:
        pass


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Provides a real AsyncSession bound to the migrated container DB.

    After each test every table (except alembic_version) is truncated, so
    tests stay isolated from each other — the container persists for the whole
    session, so committed rows would otherwise leak between tests. Truncating
    dynamically means new tables are covered automatically.
    """
    engine = create_async_engine(_ASYNC_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
            result = await session.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' "
                    "AND tablename != 'alembic_version'"
                )
            )
            tables = [row[0] for row in result.fetchall()]
            if tables:
                joined = ", ".join(f'"{t}"' for t in tables)
                await session.execute(
                    text(f"TRUNCATE {joined} RESTART IDENTITY CASCADE")
                )
                await session.commit()
    await engine.dispose()