"""
Alembic migrations environment.
Configured for async SQLAlchemy with Libex models.
"""

# Standard library
import asyncio
from logging.config import fileConfig

# Third party
from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# Local
from app.core.config import get_settings
from app.db.base import Base
from app.db.models import (  # noqa: F401 - import all models to register with Base
    Cache,
    Book,
    Author,
    Series,
    Narrator,
    Genre,
    Track,
)

settings = get_settings()

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a database connection."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations with an async engine."""
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()