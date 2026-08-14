"""
Cache manager.
Provides get/set/delete operations against the Postgres cache table.

DESIGN PHILOSOPHY: Audible-first.
The write is never gated behind a read flag: every successful Audible fetch
is stored here unconditionally, on the same path regardless of how the read
side is being used. That is what keeps the cache populated for the moment
it is actually needed — an Audible outage — and it must stay that way.

The read side defaults to fallback-only for Audible-backed keys: a service
calls Audible first and only reads its cache entry after Audible fails, so
a stale cache is never served while Audible itself is healthy. A caller may
opt a given Audible-backed key into reading cache-first instead, via its own
`use_cache` parameter (e.g. `get_author`, `get_books_by_asins`) — that is a
per-call-site choice, not a property of the key.

Some values read cache-first unconditionally, with no flag either way,
because fallback-only would be the wrong default for them rather than a
stricter one: date-derived scans like new releases and coming soon, where
the result is valid until the next UTC midnight regardless of Audible's
health, and DB-sourced values like the stats key, which have no upstream to
be authoritative over and no outage to fall back from in the first place.
"""

# Standard library
from datetime import datetime, timezone, timedelta
from typing import Any

# Third party
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert


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


def new_releases_key(region: str, days: int, category: str | None = None) -> str:
    return f"new_releases:{region}:{days}:{category or 'all'}"


def coming_soon_key(region: str, days: int, category: str | None = None) -> str:
    return f"coming_soon:{region}:{days}:{category or 'all'}"


def stats_key() -> str:
    return "db_stats"


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
        logger.info("Cache miss", extra={"cacheKey": key})
        return None

    logger.info("Cache hit", extra={"cacheKey": key})
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
    logger.info("Cache set", extra={"cacheKey": key, "ttl": ttl})


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

    Deletes directly by the expiry predicate rather than collecting keys and
    deleting by an IN list — an IN over every expired key blows past asyncpg's
    32,767 bind-parameter limit once the cache is large, which was failing the
    purge outright. A predicate delete has no per-row parameters, so it holds at
    any size. The row count comes from the DELETE's own rowcount.
    """
    result = await session.execute(
        delete(Cache).where(Cache.expires_at <= datetime.now(timezone.utc))
    )
    await session.commit()

    count = result.rowcount
    logger.info(f"Purged {count} expired cache entries")
    return count