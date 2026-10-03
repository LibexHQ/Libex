"""
Database reader service.
Reads from relational tables and reconstructs full response dicts.

Used as fallback when Audible is unavailable.
Returns the same dict format as the Audible services.

The reads themselves live in libex_core.storage.read and raise on failure.
What is hosted here is the policy around them: a failed read is logged without
caller text and answered with an empty value, so a database blip never becomes
a 500 on a route that can fall back to Audible. Every function keeps the name
and signature it has always had.
"""

# Standard library
import functools
import inspect
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

# Third party
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.models import CatalogGenre

# Services
from app.services.cache import manager as cache
from app.services.db.writer import _failure_fields

# Core
from app.core.logging import get_logger
from libex_core.storage.read import books as _books
from libex_core.storage.read import people as _people
from libex_core.storage.read import series as _series
from libex_core.storage.read.shapes import (
    audible_link as _audible_link,
    book_to_dict as _book_to_dict,
    narrator_to_dict as _narrator_to_dict,
    series_positions as _get_series_positions,
    utc_z as _utc_z,
)
from libex_core.storage.read.stats import count_stored

logger = get_logger()

# The underscore names are not public; they stay importable because the shape
# tests and the parity checks reach for them under these names.
__all__ = [
    "DbStatsResult",
    "STATS_CACHE_TTL_SECONDS",
    "get_author_book_asins_from_db",
    "get_author_books_from_db",
    "get_author_from_db",
    "get_book_from_db",
    "get_books_by_plan_from_db",
    "get_books_by_sku_from_db",
    "get_books_from_db",
    "get_coming_soon_from_db",
    "get_db_stats",
    "get_distinct_genres_from_db",
    "get_distinct_plans_from_db",
    "get_narrator_books_from_db",
    "get_new_releases_from_db",
    "get_series_books_from_db",
    "get_series_from_db",
    "get_stored_genres",
    "get_track_from_db",
    "get_vvab_books_from_db",
    "search_books_from_db",
    "search_narrators_from_db",
    "search_series_from_db",
    "_audible_link",
    "_book_to_dict",
    "_get_series_positions",
    "_narrator_to_dict",
    "_utc_z",
]


# ============================================================
# SWALLOW-AND-LOG
# ============================================================

def _guarded(message, fallback, log=(), failure=_failure_fields):
    """
    Wraps a core read so a failure is logged and answered with `fallback()`.

    `log` names the arguments that may appear in the log line, alongside
    `failure(e)`. Nothing else the caller passed is logged: search and filter
    text arrives from the query string and stays out, and failure fields never
    render the exception's own text. The core function's own signature and
    docstring carry through, so callers see the same function they always did.
    """
    def decorate(core):
        signature = inspect.signature(core)

        @functools.wraps(core)
        async def wrapper(session, *args, **kwargs):
            call = core(session, *args, **kwargs)
            try:
                return await call
            except Exception as e:
                bound = signature.bind(session, *args, **kwargs).arguments
                extra = {name: bound[name] for name in log if name in bound}
                logger.warning(message, extra={**extra, **failure(e)})
                return fallback()

        wrapper.__doc__ = (
            (core.__doc__ or "").rstrip()
            + "\n\n    A failed read is logged and answered with "
            + repr(fallback())
            + ".\n    "
        )
        return wrapper

    return decorate


def _author_failure(e: BaseException) -> dict:
    return {"error_type": type(e).__name__, "error": str(e)}


def _error_type_only(e: BaseException) -> dict:
    return {"error_type": type(e).__name__}


# ============================================================
# BOOK READERS
# ============================================================

get_book_from_db = _guarded(
    "DB read failed for book", lambda: None, ("asin", "region")
)(_books.get_book)
get_books_from_db = _guarded(
    "DB read failed for books", list, ("asins", "region")
)(_books.get_books)
search_books_from_db = _guarded("DB search failed for books", list)(_books.search_books)
get_books_by_sku_from_db = _guarded(
    "DB read failed for sku_group", list, ("sku_group",)
)(_books.get_books_by_sku)
get_distinct_plans_from_db = _guarded(
    "DB read failed for distinct plans", list
)(_books.distinct_plans)
# search is caller-supplied filter text and stays out of the log: it is not in
# the message, and _failure_fields(e) never renders the exception's own text.
get_distinct_genres_from_db = _guarded(
    "DB read failed for distinct genres", list
)(_books.distinct_genres)
get_books_by_plan_from_db = _guarded(
    "DB read failed for plan", list, ("plan_name",)
)(_books.get_books_by_plan)
get_vvab_books_from_db = _guarded("DB read failed for VVAB books", list)(_books.get_vvab_books)
get_new_releases_from_db = _guarded(
    "DB read failed for new releases", list
)(_books.get_new_releases)
get_coming_soon_from_db = _guarded(
    "DB read failed for coming soon", list
)(_books.get_coming_soon)
get_track_from_db = _guarded(
    "DB read failed for track", lambda: None, ("asin", "region")
)(_books.get_track)


# ============================================================
# AUTHOR AND NARRATOR READERS
# ============================================================

get_author_from_db = _guarded(
    "DB read failed for author", lambda: None, ("asin", "region"), _author_failure
)(_people.get_author)
# None, not [], on failure: a caller feeding this into an authoritative write
# must be able to tell a failed read from an author with no stored books.
get_author_book_asins_from_db = _guarded(
    "DB read failed for author book asins",
    lambda: None,
    ("author_asin", "region"),
    _error_type_only,
)(_people.get_author_book_asins)
get_author_books_from_db = _guarded(
    "DB read failed for author books", list, ("author_asin",)
)(_people.get_author_books)
# The searched-for name and every book filter arrive from the query string and
# are never written to a log: absent from the message, and from the extra
# fields, which carry only _failure_fields(e). The name is also a bound
# parameter, and hide_parameters on the engine (see app/db/session.py) keeps a
# StatementError from rendering it either, so it stays out twice over.
search_narrators_from_db = _guarded(
    "DB read failed for narrator search", list
)(_people.search_narrators)
get_narrator_books_from_db = _guarded(
    "DB read failed for narrator books", list
)(_people.get_narrator_books)


# ============================================================
# SERIES READER
# ============================================================

get_series_from_db = _guarded(
    "DB read failed for series", lambda: None, ("asin", "region")
)(_series.get_series)
# Series name is caller-supplied search text and is not logged, for the same
# reason as the narrator reads above.
search_series_from_db = _guarded("DB search failed for series", list)(_series.search_series)
get_series_books_from_db = _guarded(
    "DB read failed for series books", list, ("series_asin",)
)(_series.get_series_books)


# ============================================================
# STATS READER
# ============================================================

# Public, unauthenticated, and hit continuously on every README render -- the
# counters there are /db/stats/badge SVGs (app/api/routes/db/badge.py) drawn
# from this same accessor and the same cache entries, so a badge fetch costs
# what a JSON fetch costs. Stats change continuously as the seeder writes, so a
# short TTL trades a few minutes of badge staleness for skipping five
# full-table scans on most requests.
STATS_CACHE_TTL_SECONDS = 300

# The exact key set a cached stats entry must have. Guards against a cached
# value written by older code silently rendering zero for a stat added later:
# a hit whose keys don't match this set is treated as a miss and recomputed,
# rather than served as-is with StatsResponse's per-field `= 0` default
# papering over the gap. The region-scoped set carries one extra key —
# seriesRegionUnknown — since a region-scoped cache entry has no meaning under
# the global key set and vice versa.
_STAT_KEYS = frozenset({
    "books", "distinctBookAsins", "authors", "narrators", "series", "booksWithChapters",
})
_REGION_STAT_KEYS = _STAT_KEYS | {"seriesRegionUnknown"}


class DbStatsResult(NamedTuple):
    """Stats counts together with the cache expiry backing them.

    Exists so /db/stats can derive Cache-Control from the one cache read/
    write this function already does, instead of a second, independent
    cache.get_entry call in the router. Two reads of the same key cannot
    agree on "healthy, just written" versus "degraded, deliberately not
    written" — both look identical to a second read that simply finds
    nothing there. Carrying the expiry out of the read this function already
    performed removes the second read entirely, so there is nothing left to
    disagree.

    cache_expires_at is None whenever the returned stats are not backed by a
    live cache entry: the live query itself failed (the all-zeros fallback),
    or it succeeded but the follow-up cache.set failed, so no entry exists
    for any read to find. The router should treat None the way
    _mark_completeness (app/api/routes/authors/router.py) treats an
    incomplete result — Cache-Control: no-store — never substitute
    STATS_CACHE_TTL_SECONDS as a guessed duration, which is exactly the bug
    this replaces: the fallback was reachable only on failure and handed out
    the longest freshness Libex offers.

    On a fresh write, cache_expires_at is computed locally as
    now + STATS_CACHE_TTL_SECONDS rather than re-read back from the row
    cache.set just committed, to avoid a third round trip for a value this
    function already knows deterministically. The row's real expiry is set
    a few microseconds later inside cache.set, so this is a lower bound —
    the edge is told to hold the value for very slightly less than it
    actually remains valid, never more.
    """
    stats: dict[str, int]
    cache_expires_at: datetime | None



async def get_db_stats(
    session: AsyncSession, region: str | None = None, refresh: bool = False
) -> DbStatsResult:
    """
    Returns counts of books (stored records, one per asin and region),
    distinctBookAsins, authors, narrators, series, and books with stored
    chapter data in the local DB, together with the cache expiry backing that
    value (see DbStatsResult). booksWithChapters counts rows in the tracks
    table (one per book that actually has chapters stored), not books that have
    merely been checked — checked includes ISBN-keyed records and bundle ASINs
    that will never have chapters, which would overstate what Libex holds.

    `region=None` (the default) returns the global counts under the one global
    cache key. The keys the figure has always had keep their meaning;
    distinctBookAsins is the one added, the number of ASINs among the stored
    book records, which is smaller than books whenever a book is stored under
    more than one region.

    Passing a region scopes books, authors, booksWithChapters and series to
    it. narrators cannot follow: it has no region column at all — the name is
    the primary key, and a narrator is not owned by any one marketplace — so
    a region-scoped call still returns the global narrator count rather than
    a wrong or empty per-region figure. A region-scoped response also carries
    seriesRegionUnknown, always 0 now that a series row's region is part of
    its key, so the shape does not change under a caller.

    booksWithChapters is scoped by joining tracks to books on asin and region.

    Cached for STATS_CACHE_TTL_SECONDS, one entry per region plus one for the
    global figure — see cache.stats_key(). A cache miss falls back to the live
    query, and so does a cache error — but a cache error at the database level
    leaves the session's transaction aborted, so it must be rolled back before
    the live query can run on the same session, and a cache write failure
    never fails the request (but does mean the returned DbStatsResult carries
    no cache_expires_at, since nothing was actually written for it to quote).
    A DB read failure is rolled back too, and so is a cache WRITE failure —
    all three handlers now leave the session usable, which is the property
    that matters once a caller reuses one session across several calls. The
    write handler was the last one missing it, and on the request path it
    read as harmless because the session is closed immediately afterwards;
    the background refresher (app/services/db/stats_refresh.py) walks up to
    twelve entries on one session, where an aborted transaction left behind
    by entry three fails entry four on InFailedSqlTransaction and counts a
    healthy entry as a failure.

    All three handlers report through _failure_fields for the same reason
    the refresher does. They used to interpolate the exception, which reads
    as adequate on the request path because a caller saw the failure too --
    but that refresher calls them unattended, up to twelve times a pass, and
    the exceptions a degraded database produces here include ones whose
    str() is empty. A bare TimeoutError from a partitioned host logged a
    line that named no cause at all. error_type and the SQLSTATE name it
    without putting the exception text, and whatever Postgres embedded in
    it, into the log.

    `refresh=True` skips the cache read and always runs the live query,
    writing the result over whatever is stored. It exists for the background
    refresher (app/services/db/stats_refresh.py), whose whole job is to
    replace an entry that is still live, before it lapses — a cache-aside
    read would hand it back the very value it is there to supersede. It
    deliberately does not invalidate the key first: on a DB failure the
    handler below returns the all-zeros fallback *before* cache.set is
    reached, so nothing is written and the stored entry keeps its real
    value for whatever life it had left. Only that remainder, though —
    cache.get_entry filters on expires_at > now, so from the moment the
    entry lapses the request path treats it as a miss and recomputes
    instead of handing the stored value back, and what covers a reader
    past that point is the edge's stale-if-error window
    (app/api/routes/db/stats_headers.py), not this row. Clearing the key
    would trade even that away, and for nothing — the write here is an
    upsert, so a successful refresh overwrites the entry either way.
    """
    key = cache.stats_key(region)
    expected_keys = _STAT_KEYS if region is None else _REGION_STAT_KEYS
    try:
        entry = None if refresh else await cache.get_entry(session, key)
        if entry is not None and set(entry.value) == expected_keys:
            return DbStatsResult(entry.value, entry.expires_at)
    except Exception as e:
        logger.warning("Cache read failed for stats", extra={"region": region, **_failure_fields(e)})
        await session.rollback()

    try:
        stats = await count_stored(session, region)
    except Exception as e:
        logger.warning("DB read failed for stats", extra={"region": region, **_failure_fields(e)})
        await session.rollback()
        fallback = {
            "books": 0,
            "distinctBookAsins": 0,
            "authors": 0,
            "narrators": 0,
            "series": 0,
            "booksWithChapters": 0,
        }
        if region is not None:
            fallback["seriesRegionUnknown"] = 0
        return DbStatsResult(fallback, None)

    cache_expires_at = datetime.now(timezone.utc) + timedelta(seconds=STATS_CACHE_TTL_SECONDS)
    try:
        await cache.set(session, key, stats, ttl_seconds=STATS_CACHE_TTL_SECONDS)
    except Exception as e:
        logger.warning("Cache write failed for stats", extra={"region": region, **_failure_fields(e)})
        await session.rollback()
        cache_expires_at = None

    return DbStatsResult(stats, cache_expires_at)


async def get_stored_genres(
    session: AsyncSession, region: str
) -> tuple[list[dict[str, str]], datetime | None]:
    """
    Returns stored catalog genres for a region and the oldest last_checked
    timestamp among them, so callers can decide whether the stored set is stale
    and needs refreshing. Each entry carries parent_id ("" for a top-level
    parent, the parent's id for a leaf). Returns ([], None) when no genres are
    stored yet.
    """
    try:
        result = await session.execute(
            select(
                CatalogGenre.genre_id,
                CatalogGenre.parent_id,
                CatalogGenre.name,
                CatalogGenre.last_checked,
            )
            .where(CatalogGenre.region == region)
            .order_by(CatalogGenre.name.asc())
        )
        rows = result.fetchall()
        if not rows:
            return [], None
        genres = [{"genre_id": r[0], "parent_id": r[1], "name": r[2]} for r in rows]
        oldest_checked = min(r[3] for r in rows)
        return genres, oldest_checked
    except Exception as e:
        logger.warning(
            "DB read failed for catalog_genres",
            extra={"region": region, **_failure_fields(e)},
        )
        return [], None
