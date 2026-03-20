# Standard library
from collections.abc import AsyncGenerator

# Third party
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Local
from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=settings.database_echo,
    pool_pre_ping=True,
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