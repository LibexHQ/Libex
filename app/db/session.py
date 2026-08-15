# Standard library
from collections.abc import AsyncGenerator

# Third party
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Local
from app.core.config import get_settings

settings = get_settings()

# One uvicorn worker per deployment, so this pool is the only consumer of
# Postgres connections for the API, the seeder loop, and every background
# writer task. Sized against Postgres's own default max_connections=100
# (unaltered in docker-compose.yml): pool_size + max_overflow caps this
# process at 40 connections, leaving comfortable headroom for admin
# sessions, migrations, and Postgres's reserved superuser slots.
# server_settings apply per-connection at the driver level: statement_timeout
# bounds any single query so a runaway statement can't pin a connection
# indefinitely, and idle_in_transaction_session_timeout backstops a session
# left idle-in-transaction (e.g. holding a lock across a slow network await),
# which would otherwise pin the xmin horizon and block autovacuum on a
# 1.1M-row, write-heavy table.
# hide_parameters keeps caller-supplied search text out of the logs. Every
# search filter is a bound parameter, and SQLAlchemy renders a statement's
# bound parameters into str() on any StatementError -- which every DBAPIError,
# OperationalError and DataError is a subclass of. So a query that times out
# against the 30s statement_timeout above, or fails for any other reason, would
# otherwise print what the caller typed as part of the exception text, no matter
# how carefully the surrounding log message avoids it. This suppresses that
# rendering for every statement, including ones not yet written. The compiled
# SQL and the driver's own message are still logged, so a failure is still
# diagnosable down to the statement that caused it; only the literal values go.
engine = create_async_engine(
    settings.database_url,
    echo=settings.database_echo,
    hide_parameters=True,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=20,
    pool_timeout=30,
    connect_args={
        "server_settings": {
            "statement_timeout": "30000",
            "idle_in_transaction_session_timeout": "60000",
        }
    },
)

AsyncSessionFactory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides a database session."""
    async with AsyncSessionFactory() as session:
        yield session