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


async def get_many(session: AsyncSession, keys: list[str]) -> dict[str, Any]:
    """
    Retrieves many cached values, one query per 5000 keys, as {key: value}.

    Only live entries appear in the result: a key that is absent, or present
    but expired, is simply not in the returned dict, so `result.get(key)`
    yields None for exactly the keys `get` would have returned None for. The
    expiry predicate is the same one `get` uses, evaluated once for the whole
    batch rather than once per key -- a single point in time across the batch,
    where a per-key loop over `get` drifts its own `now` forward as it goes
    and can call the same entry live at the top of a list and expired at the
    bottom. `now` is taken before the first chunk for that reason, so the
    batch stays one instant however many chunks it takes.

    Exists because a per-ASIN loop over `get` costs one database round trip
    per key. Duplicate keys are collapsed before the query; the caller reads
    values back by key, so a repeated key resolves to the same entry.

    One aggregate log line for the whole call instead of `get`'s per-key
    hit/miss pair: at a thousand keys the per-key form buries every other
    line in the log for that request.

    Chunked at 5000, the size reader._get_series_positions_batch and the
    seeder use against the same ceiling -- one bind per key against asyncpg's
    32,767 bind parameters, the limit that broke purge_expired (see below).
    The key list is not bounded by the bulk book route's 1000-ASIN cap. Both
    call sites are in get_books_by_asins, and the author routes reach it with
    a whole author catalogue: get_author_books applies no limit of its own,
    and the largest measured author is 4164 books. The fallback site is what
    settles the size question -- it sits inside that function's outage
    handler and runs on any Audible failure whether or not `use_cache` was
    asked for, so a default `cache=false` author request is precisely the one
    that arrives here with thousands of keys. Unchunked, a large enough list
    would raise from inside the handler whose whole job is to degrade
    gracefully, turning an Audible outage into a 500 where the per-key loop
    merely ran slowly.
    """
    if not keys:
        return {}

    unique_keys = list(dict.fromkeys(keys))
    now = datetime.now(timezone.utc)

    hits: dict[str, Any] = {}
    for i in range(0, len(unique_keys), 5000):
        chunk = unique_keys[i:i + 5000]
        result = await session.execute(
            select(Cache.key, Cache.value).where(
                Cache.key.in_(chunk),
                Cache.expires_at > now,
            )
        )
        hits.update({row.key: row.value for row in result})

    logger.info("Cache batch lookup", extra={
        "requested": len(unique_keys),
        "hits": len(hits),
        "misses": len(unique_keys) - len(hits),
    })
    return hits


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