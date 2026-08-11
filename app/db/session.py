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
engine = create_async_engine(
    settings.database_url,
    echo=settings.database_echo,
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