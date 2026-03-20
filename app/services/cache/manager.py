"""
Cache manager.
Provides get/set/delete operations against the Postgres cache table.

DESIGN PHILOSOPHY: Audible-first.
Cache is a fallback only. Services call Audible first,
store results here, and fall back to cache on Audible failure.
"""

# Standard library
from datetime import datetime, timezone, timedelta
from typing import Any, cast

# Third party
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine.cursor import CursorResult


# Database
from app.db.models import Cache

# Core
from app.core.config import get_settings
from app.core.logging import get_logger

settings = get_settings()
logger = get_logger()


# ============================================================
# KEY BUILDERS
# ============================================================

def book_key(asin: str, region: str) -> str:
    return f"book:{region}:{asin}"


def books_bulk_key(asins: list[str], region: str) -> str:
    joined = "+".join(sorted(asins))
    return f"books:{region}:{joined}"


def author_key(asin: str, region: str) -> str:
    return f"author:{region}:{asin}"


def author_books_key(asin: str, region: str) -> str:
    return f"author_books:{region}:{asin}"


def series_key(asin: str, region: str) -> str:
    return f"series:{region}:{asin}"


def series_books_key(asin: str, region: str) -> str:
    return f"series_books:{region}:{asin}"


def search_key(query: str, region: str) -> str:
    normalized = query.lower().strip().replace(" ", "+")
    return f"search:{region}:{normalized}"


def chapters_key(asin: str, region: str) -> str:
    return f"chapters:{region}:{asin}"


# ============================================================
# CACHE OPERATIONS
# ============================================================

async def get(session: AsyncSession, key: str) -> Any | None:
    """
    Retrieves a cached value by key.
    Returns None if not found or expired.
    """
    result = await session.execute(
        select(Cache).where(
            Cache.key == key,
            Cache.expires_at > datetime.now(timezone.utc),
        )
    )
    entry = result.scalar_one_or_none()

    if entry is None:
        logger.debug(f"Cache miss: {key}")
        return None

    logger.debug(f"Cache hit: {key}")
    return entry.value


async def set(
    session: AsyncSession,
    key: str,
    value: Any,
    ttl_seconds: int | None = None,
) -> None:
    """
    Stores a value in the cache.
    Uses upsert so repeated writes to the same key just refresh it.
    TTL defaults to settings.cache_ttl if not specified.
    """
    ttl = ttl_seconds if ttl_seconds is not None else settings.cache_ttl
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

    stmt = insert(Cache).values(
        key=key,
        value=value,
        created_at=datetime.now(timezone.utc),
        expires_at=expires_at,
    ).on_conflict_do_update(
        index_elements=["key"],
        set_={
            "value": value,
            "created_at": datetime.now(timezone.utc),
            "expires_at": expires_at,
        }
    )

    await session.execute(stmt)
    await session.commit()
    logger.debug(f"Cache set: {key} (ttl={ttl}s)")


async def invalidate(session: AsyncSession, key: str) -> None:
    """Deletes a specific cache entry by key."""
    await session.execute(delete(Cache).where(Cache.key == key))
    await session.commit()
    logger.debug(f"Cache invalidated: {key}")


async def purge_expired(session: AsyncSession) -> int:
    """
    Deletes all expired cache entries.
    Returns the number of rows deleted.
    Intended to be called on a schedule.
    """
    result = await session.execute(
        select(Cache.key).where(
            Cache.expires_at <= datetime.now(timezone.utc)
        )
    )
    keys = [row[0] for row in result.fetchall()]
    count = len(keys)

    if keys:
        await session.execute(delete(Cache).where(Cache.key.in_(keys)))
        await session.commit()

    logger.info(f"Purged {count} expired cache entries")
    return count