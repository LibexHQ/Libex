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
import asyncio
import random
from datetime import datetime, timezone, timedelta
from typing import Any

# Third party
from asyncpg.exceptions import TransactionRollbackError
from sqlalchemy import select, delete, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert


# Database
from app.db.models import Cache

# Core
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.middleware import is_safe_log_value

settings = get_settings()
logger = get_logger()

# Every key builder below composes only region, ASIN and enum-bounded
# arguments -- all validated before they reach here -- so a key is safe by
# construction and this rarely fires. It stays as the backstop for the one
# place that isn't: get/set/invalidate take a bare `key` from any caller,
# and the guarantee upstream validation gives a specific builder does not
# extend to this function's signature. Reuses is_safe_log_value rather than
# a second rule, so the two cannot drift apart.
_REDACTED_KEY = "REDACTED"


def _safe_key_for_log(key: str) -> str:
    """Returns a cache key as-is for logging if it is safe, else the sentinel."""
    return key if is_safe_log_value(key) else _REDACTED_KEY


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
        logger.info("Cache miss", extra={"cacheKey": _safe_key_for_log(key)})
        return None

    logger.info("Cache hit", extra={"cacheKey": _safe_key_for_log(key)})
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
    logger.info("Cache set", extra={"cacheKey": _safe_key_for_log(key), "ttl": ttl})


async def invalidate(session: AsyncSession, key: str) -> None:
    """Deletes a specific cache entry by key."""
    await session.execute(delete(Cache).where(Cache.key == key))
    await session.commit()
    logger.debug("Cache invalidated", extra={"cacheKey": _safe_key_for_log(key)})


# Rows removed per purge_expired pass. Measured directly against a throwaway
# Postgres 16.14 instance, cache table as defined, non-compressible payloads
# sized to the ~3.3KB a normalized book measures serialized: an unbatched
# DELETE over every expired row took 3.83s at 200k rows and 25.60s at 600k —
# superlinear, and dominated by row and TOAST deletion rather than the scan
# (600k dirtied 306,197 buffers against 6,186 for the seq scan). Restoring the
# expires_at index dropped in 22195f2af627 does not help — the plan is
# correctly a seq scan when everything is expired, and the cost measured
# above it, not the lookup. A corpus refresh over 1.1M+ books writes one
# cache row per book at the default TTL, so a single purge can face the whole
# cache expiring inside one hourly window — comfortably past
# session.py's statement_timeout of 30s on the unbatched form, which fails
# every attempt identically and purges nothing, forever.
#
# 10,000 selected by ctid, not a WHERE IN of keys — ctid carries no per-row
# bind parameter, so it cannot reintroduce the bind-limit failure the
# predicate delete below it was already written to avoid. Measured at 94ms,
# two orders of magnitude inside the timeout.
#
# That 94ms is the FIRST batch on an all-expired table, where the ten
# thousand qualifying rows sit in the leading pages. It is not what batch
# eighty costs. Each batch is its own statement, so without a cursor every
# batch re-reads the dead-tuple prefix the batches before it already
# cleared, and a 1.1M-row purge becomes a hundred-odd scans over a growing
# prefix where the unbatched form paid exactly one. Hence :after below: the
# pass sweeps ctid ascending and never looks at a page twice.
#
# The claim this replaces — that restoring the expires_at index dropped in
# 22195f2af627 "does not help" — was true only of the unbatched DELETE it
# was measured against, where everything is expired and a seq scan is the
# right plan. It was never true of the batched form underneath it, and left
# standing it would have talked the next reader out of the fix. The index
# is still not restored, and deliberately: expires_at changes on every
# cache.set, so indexing it makes every write non-HOT on the most
# write-heavy table Libex has. The cursor costs nothing on either side.
_PURGE_BATCH_SIZE = 10000

# Selected and deleted as two statements so the sweep can carry a cursor,
# with the expiry predicate repeated on the DELETE. The repetition is not
# redundant: between the two statements a concurrent writer can refresh one
# of the selected rows, and without the predicate the delete would remove an
# entry that is live again. With it, that row is spared exactly as the
# single-statement form spared it.
# The double cast is not decoration: asyncpg ships no codec for tid, so a
# bound parameter cannot be a tid directly — it raises "invalid input for
# query" — and casting text to tid inside the statement is what lets the
# cursor be a parameter at all. Verified against Postgres 16; the single-cast
# and tid[] forms were both tried and both fail to bind.
_PURGE_CANDIDATES_STATEMENT = text(
    "SELECT ctid FROM cache "
    "WHERE ctid > CAST(CAST(:after AS text) AS tid) AND expires_at <= :now "
    "ORDER BY ctid LIMIT :batch_size"
)

# Deleted as a ctid RANGE rather than a list of the selected ctids, which
# needs no array bind and is exactly equivalent: the select took every
# expired row in ctid order from :after up to :last, so no expired row inside
# that range was skipped. The expiry predicate is repeated because a
# concurrent writer can refresh one of those rows between the two statements,
# and without it the delete would remove an entry that is live again.
_PURGE_DELETE_STATEMENT = text(
    "DELETE FROM cache "
    "WHERE ctid > CAST(CAST(:after AS text) AS tid) "
    "AND ctid <= CAST(CAST(:last AS text) AS tid) "
    "AND expires_at <= :now"
)

# Where a pass starts. Postgres's first possible tuple is (0,1), so this
# sorts below every real row.
_PURGE_CURSOR_START = "(0,0)"

# Attempts per batch against a deadlock or serialization failure, and the
# first backoff step between them, doubled per attempt and jittered across
# half to one and a half — same shape as persist_queue's chunk retry, kept
# local instead of imported so this module gains no edge onto persist_queue
# (which already imports this one). Measured under a corpus refresh's write
# rate: 7 deadlocks in ~12 seconds against four concurrent _cache_set_many
# writers, three of which would otherwise have killed a purger on its first
# batch. Nothing about the collision is expected to repeat on a retry — the
# writers bind in insertion order, the DELETE's Tid Scan follows a
# HashAggregate's bucket order, and the two correlate with nothing about each
# other from one attempt to the next.
_PURGE_MAX_ATTEMPTS = 3
_PURGE_RETRY_BASE_SECONDS = 0.25


async def _purge_batch(session: AsyncSession, now: datetime, after: str) -> tuple[int, str | None]:
    """
    Executes and commits one purge batch, retrying a deadlock or
    serialization failure against the same candidate rows before giving up.

    Returns (rows deleted, cursor to resume from). A None cursor means the
    sweep reached the end of the table and the pass is over.

    The cursor is the last ctid SELECTED, not the last one deleted, and the
    difference matters: a row refreshed between the two statements is spared
    by the DELETE's own expiry predicate, and advancing past it is what keeps
    the pass linear. It is expired again only if it lapses a second time, by
    which point the next hourly pass collects it. Advancing to the last
    DELETED ctid instead would re-read every spared row on the following
    batch, which is the behaviour this cursor exists to remove.

    Raises the final attempt's DBAPIError rather than swallowing it — every
    batch before this one already committed regardless, so purge_expired is
    the one that decides whether stopping the pass there is acceptable.
    """
    for attempt in range(1, _PURGE_MAX_ATTEMPTS + 1):
        try:
            candidates = await session.execute(
                _PURGE_CANDIDATES_STATEMENT,
                {"now": now, "after": after, "batch_size": _PURGE_BATCH_SIZE},
            )
            # Postgres renders a ctid as "(0, 1)"; its text input accepts no
            # space, so the round trip has to be normalised or the cast
            # below rejects it.
            ctids = [str(row[0]).replace(" ", "") for row in candidates]
            if not ctids:
                await session.rollback()
                return 0, None

            last = ctids[-1]
            deleted = await session.execute(
                _PURGE_DELETE_STATEMENT, {"now": now, "after": after, "last": last}
            )
            await session.commit()
            return deleted.rowcount, last
        except DBAPIError as exc:
            await session.rollback()
            retryable = isinstance(getattr(exc, "orig", None), TransactionRollbackError)
            if attempt == _PURGE_MAX_ATTEMPTS or not retryable:
                raise
            nominal = _PURGE_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            await asyncio.sleep(nominal * (0.5 + random.random()))


async def purge_expired(session: AsyncSession) -> int:
    """
    Deletes all expired cache entries, one bounded batch at a time.
    Returns the total rows deleted so far. Intended to be called on a
    schedule.

    A single unbatched DELETE over the whole table risks statement_timeout
    once enough rows are expired at once — see _PURGE_BATCH_SIZE — which
    rolls back the entire statement and purges nothing. Each batch commits on
    its own instead, so a timeout, a deadlock, or a restart mid-purge keeps
    whatever already committed rather than losing all progress, and no single
    statement holds the connection anywhere near the timeout.

    Selecting by ctid rather than an IN list of keys is also what keeps this
    safe under _cache_purge_loop running uncoordinated in all six workers: a
    row a concurrent purge already deleted is simply not selected again by
    the next one, never a duplicate-delete error, and rowcount reflects only
    what this call actually removed.

    Loops until a pass deletes nothing — not until a pass returns fewer than
    a full batch. A short batch is not proof the candidate set is empty: a
    row refreshed between the SELECT's snapshot and the DELETE fails
    ctid IN (...)'s EvalPlanQual recheck and is correctly spared, and a
    corpus refresh's write rate makes that the norm rather than the
    exception — measured, one purger against one modest writer: a batch of
    10,000 candidates delivered 9,996 spared-and-short, ending the pass with
    88,110 genuinely expired rows still in place. now is bound once before
    the loop, so the candidate set can only shrink across passes — deleted
    rows leave it, refreshed rows leave it — which is what still guarantees
    termination without the short-batch shortcut.

    A batch that still can't complete after _purge_batch's own retries stops
    the pass here rather than raising out to _cache_purge_loop's hourly
    retry — every earlier batch's commit already stands, so the loss is only
    this pass's remainder, picked up again on the next scheduled run.
    """
    total = 0
    now = datetime.now(timezone.utc)
    cursor = _PURGE_CURSOR_START
    while True:
        try:
            deleted, cursor = await _purge_batch(session, now, cursor)
        except DBAPIError as exc:
            logger.warning(
                "Cache purge batch failed, stopping this pass",
                extra={
                    "purged": total,
                    "error_type": type(exc).__name__,
                    "sqlstate": getattr(getattr(exc, "orig", None), "sqlstate", None),
                },
            )
            break

        # A None cursor is the sweep reaching the end of the table, which is
        # the only exit that means "nothing expired remains". A batch that
        # deleted nothing is NOT that: every one of its candidates can have
        # been refreshed between the select and the delete, which is the
        # spared-row case the module has always had to tolerate, and the
        # sweep must carry on past them rather than stopping at the first
        # contended page.
        if cursor is None:
            break

        # A driver is not obliged to report a row count — the DBAPI
        # convention for "unavailable" is -1 — and that would corrupt the
        # total rather than the loop, since the cursor now governs
        # termination. Clamped rather than trusted.
        if deleted < 0:
            deleted = 0

        total += deleted

    if total:
        logger.info(f"Purged {total} expired cache entries")
    return total