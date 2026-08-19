"""
Audible authors service.
Fetches author metadata directly from the Audible API.

DESIGN PHILOSOPHY: Audible-first.
Always fetches fresh data from Audible.
Writes every result to the relational DB for persistence.
Falls back to DB when Audible is unavailable.
"""

# Standard library
import asyncio
import time
from datetime import datetime, timezone
from typing import Any, NamedTuple

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Core
from app.core.exceptions import NotFoundException
from app.core.logging import get_logger
from app.core.utils import strip_html

# Services
from app.services.audible.client import audible_get, author_books_concurrency, LOCALE_MAP
from app.services.audible.authors.screens import (
    _fetch_author_books_by_screen,
    _ScreenBooksResult,
    SCREENS_REASON_PLATEAU_TRUNCATED,
    SCREENS_BROKEN_REASONS,
)
from app.services.audible.authors.catalog import (
    _fetch_author_books_by_catalog,
    _CatalogBooksResult,
    fetch_author_books_by_name,
)
from app.services.cache import manager as cache
from app.services.cache.manager import author_key, author_books_key
from app.services.db.persist_queue import persist_author_background, persist_author_books_cache_background
from app.services.db.reader import get_author_from_db, get_author_book_asins_from_db

# Imported for its side effect on the module-level task registry it owns.
# The edge runs one way at import time -- completion imports nothing from
# here at module scope, and reaches _walk_author_books through a deferred
# import inside the task body -- so this does not close a cycle. See that
# module's own note on why the import is deferred rather than restructured.
from app.services.audible.authors.completion import request_author_books_completion

logger = get_logger()

# Wall-clock budget across the whole author-books union in get_author_books
# (one screens walk plus one name search). This bounds the work a single
# request can do -- it is not a rate limit, counts nothing across requests,
# keys on no client identity, and rejects nobody. Unlike SCREENS_MAX_PAGES /
# SCREENS_MAX_ASINS (screens.py) and NAME_SEARCH_MAX_PAGES (catalog.py),
# this one DOES bind on a real, very prolific author's catalog before those
# far-larger caps ever would -- at roughly 0.5s/page it cuts off around 1800
# titles, well short of Conan Doyle's 4500 -- but a deadline-truncated walk
# is reported as degraded (not clean/complete) and is not cached at all --
# it is handed to authors/completion.py to be finished off the request and
# the COMPLETE result is what gets stored, so it costs latency on the prolific
# tail, not silent, permanent data loss the way the old page/ASIN caps did.
# That's a deliberate latency/completeness tradeoff, not an oversight, and
# changing it is a separate decision from the caps in screens.py and
# catalog.py.
#
# This alone is not what keeps a live author-books request under the
# fronting proxy's timeout -- it only ever bounded discovery, and hydration
# (get_books_by_asins, run after this returns) had no bound of its own at
# all, which is what actually produced a live, measured production outage:
# every one of 5 concurrent author lookups 504'd at exactly 30.1s, and even
# a single uncontended prolific-author request (Christie) measured 28.22s
# wall clock. The actual fix is client.py's AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT
# pool (entered below via author_books_concurrency) -- see that constant's
# own docstring for the measurements: raising the effective in-flight limit
# from 10 to 25-30 for this call path more than halved wall clock in the
# 5-concurrent case (12.71s at 10 vs 5.77s at 30) -- the case that produced
# the outage, and the only one the measurements resolve clear of their own
# run-to-run noise. That is what keeps a full, untruncated result inside the
# proxy's window rather than needing to return less than what Audible has.
# This deadline remains underneath that as a backstop against a genuinely
# pathological walk, not the mechanism the outage fix relies on.
#
# It is 25s, and was 45. At 45 it could never fire: the fronting proxy gives
# up at 30s, so the deadline sat beyond the point where the caller had already
# been sent a 504 and the backstop was decorative. It now bounds the WHOLE request
# -- discovery and hydration share one deadline, computed once at the route --
# and lands inside the proxy's window with room for the response to be
# assembled and written. A walk that hits it returns what it has, marked
# incomplete, instead of the caller getting a 504 and nothing.
AUTHOR_BOOKS_TIME_BUDGET_SECONDS = 25.0



class AuthorBooksResult(NamedTuple):
    """
    An author's ASIN list together with whether the walk that produced it
    actually finished.

    is_complete used to be computed inside _walk_author_books and thrown
    away -- it chose the cache TTL, it went into a log line, and nothing
    else could see it. That was survivable only while the cache served
    almost nobody. Once the cache began serving the default path, an
    unfinished walk became something Libex would hand back, store, and then
    keep handing back, with no way for the route to know and no way for a
    caller to ask. Returning it is what lets the route mark the response and
    what lets the caching decision move to a caller that can act on it.
    """
    asins: list[str]
    is_complete: bool
    # When this list came from the cache, the moment that entry stops being
    # live; None when it came from a walk just now. The route turns it into
    # the edge cache's s-maxage so a copy held at the edge expires at the
    # same instant Libex's own entry does. None is not "no expiry" -- it is
    # "this was just walked and the cache write is still in flight", which
    # the route deliberately treats as the conservative case, since a
    # background write can still fail and leave the edge holding something
    # Libex never stored.
    cache_expires_at: datetime | None = None

# Coalesces concurrent get_author_books calls for the same (asin, region)
# onto a single walk, keyed identically to the cache key -- "concurrent"
# here means "would have produced the same cache entry". Populated and
# cleared around the single await point in get_author_books below, with no
# other await between the membership check and the insert, so two requests
# arriving on the same event loop tick can never both become the leader.
# Per-process only: with one uvicorn worker today this collapses every
# concurrent request in that worker onto one walk; a second worker process
# holds its own separate map and would still run its own independent walk.
_author_books_inflight: dict[tuple[str, str], asyncio.Task[AuthorBooksResult]] = {}

# ============================================================
# HELPERS
# ============================================================

def _generate_session_id() -> str:
    """
    Generates a random session ID matching AudiMeta's format.
    Format: 000-XXXXXXX-XXXXXXX
    """
    import random

    def random_digits() -> str:
        return str(random.randint(0, 9999999)).zfill(7)

    return f"000-{random_digits()}-{random_digits()}"


def _normalize_author(data: dict, asin: str, region: str) -> dict[str, Any]:
    contributor = data.get("contributor", {})
    bio = contributor.get("bio")
    return {
        "id": None,
        "asin": asin,
        "name": contributor.get("name", "").replace("\t", "").strip(),
        "description": strip_html(bio),
        "image": contributor.get("profile_image_url"),
        "region": region,
        "regions": [region],
        "genres": [],
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================
# AUDIBLE REQUESTS
# ============================================================


async def _fetch_author_details(asin: str, region: str) -> dict[str, Any]:
    """
    Fetches author profile from Audible contributors endpoint.
    Returns bio, image, and name.
    """
    path = f"/1.0/catalog/contributors/{asin}"
    params = {
        "locale": LOCALE_MAP.get(region, "en-US"),
    }
    return await audible_get(region, path, params)


async def _resolve_author_name(
    asin: str,
    region: str,
    session: AsyncSession,
) -> str | None:
    """
    Resolves an author ASIN to their name, checking DB first and then
    Audible's contributors endpoint.

    Returns None only for the terminal, non-degraded case: Audible confirms
    this author carries no name (a 404, or a 200 with an empty name field).
    Any other failure fetching from Audible -- a timeout, a malformed
    response, anything that is not a confirmed absence -- propagates rather
    than collapsing into the same None, so the caller can tell "this author
    has no name on record" apart from "name resolution failed" instead of
    treating both as the same silent miss.
    """
    db_author = await get_author_from_db(session, asin, region)
    if db_author and db_author.get("name"):
        return db_author["name"]

    try:
        data = await _fetch_author_details(asin, region)
    except NotFoundException:
        return None

    name = data.get("contributor", {}).get("name", "").replace("\t", "").strip()
    if name:
        normalized = _normalize_author(data, asin, region)
        persist_author_background(normalized, region)
        return name

    return None


# ============================================================
# PUBLIC API
# ============================================================


async def get_author(
    asin: str,
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
) -> dict[str, Any]:
    """
    Fetches author profile by ASIN.
    Audible-first, writes to DB, falls back to DB then cache.
    """
    if use_cache:
        cached = await cache.get(session, author_key(asin, region))
        # Same reason as the two rollbacks in _walk_author_books below: a
        # connection is held for work, not for a request. The read above
        # autobegins a transaction on session, and a READ COMMITTED
        # transaction advertises backend_xmin and can become the cluster's
        # oldest xmin even when it has only ever read -- measured on
        # PostgreSQL 16.14 -- holding the xmin horizon against autovacuum
        # until it ends. Nothing between here and the contributors fetch
        # below needs it open, and that fetch queues against the process-wide
        # pool in client.py rather than going out immediately.
        #
        # Nothing after this point depends on transaction state established
        # before it: cached is a plain already-materialized value, and the DB
        # and cache reads in the failure branch each open their own
        # transaction on their next statement, which SQLAlchemy re-acquires
        # transparently.
        await session.rollback()
        if cached:
            return cached

    try:
        start = time.monotonic()
        data = await _fetch_author_details(asin, region)
        author_took = round((time.monotonic() - start) * 1000, 2)

        if not data or data.get("contributor", {}).get("name") is None:
            raise NotFoundException(f"Author not found: {asin}")

        normalized = _normalize_author(data, asin, region)

        # Persist to DB and cache in the background
        persist_author_background(normalized, region)

        logger.info("Requested Audible Author", extra={
            "author_took": author_took,
            "region": region,
        })

        return normalized

    except NotFoundException:
        raise
    except Exception:
        # Try DB first
        db_result = await get_author_from_db(session, asin, region)
        if db_result:
            return db_result

        # Fall back to cache
        cached = await cache.get(session, author_key(asin, region))
        if cached:
            return cached

        raise NotFoundException("Audible unavailable and no cached author data found")


async def get_author_books(
    asin: str,
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
    deadline: float | None = None,
) -> AuthorBooksResult:
    """
    Fetches all book ASINs for an author. See _walk_author_books for the
    four-source union this delegates to and does the actual fetching.

    use_cache=True checks the cache first and returns on a hit, same as
    every other Audible-first service in this module. Both author-books
    routes now pass True, because the public default was flipped there --
    see the comment on get_books_by_author for why a defaulted-False read
    meant the cache served only callers who explicitly asked for it and no
    repeat request was ever cheap. This signature deliberately keeps False
    so that a future direct caller opts in for its own reasons rather than
    silently inheriting a route's policy. Below that, concurrent calls for
    the same (asin, region) -- with or without use_cache, since a miss and
    an explicit cache=false arrive below identically --
    are coalesced onto a single walk via _author_books_inflight rather than
    each starting its own: the first caller for a given key becomes the
    leader and runs _walk_author_books, every other caller for that same key
    while it's running awaits the leader's own task and gets its result (or
    its exception) instead of starting a second one. A follower's session
    contributes nothing to the leader's walk, so it's released with a
    rollback before the wait rather than held idle-in-transaction against
    the pool for however long the leader's walk still has to run. A
    follower's wait is also shielded, so a follower that goes away takes
    nothing with it -- see the two awaits below for why only one of them is.
    """
    if use_cache:
        entry = await cache.get_entry(session, author_books_key(asin, region))
        if entry:
            cached = entry.value
            # is_complete=True is an invariant of the key, not an assumption
            # about this entry: _walk_author_books writes it only when the
            # walk finished, and an unfinished one goes to background
            # completion instead. Anything read back here is therefore a
            # complete result. The one exception is transitional -- entries
            # written by an older build under the degraded TTL may still be
            # in the table for up to the fifteen-minute TTL that build used
            # for a degraded write, and will read back as complete. That
            # window closes itself and needs no migration.
            return AuthorBooksResult(cached, True, entry.expires_at)

    inflight_key = (asin, region)
    leader = _author_books_inflight.get(inflight_key)
    if leader is not None:
        await session.rollback()
        # Shielded, and it has to be. Unlike Go's singleflight -- whose
        # leader runs in a goroutine no waiter can reach -- ours runs in a
        # Task every awaiter holds directly, and a plain `await leader`
        # does not insulate it: cancelling the awaiting coroutine cancels
        # the awaited Task too. So without this, one follower arriving deep
        # into a prolific author's walk and then being cancelled -- by a
        # graceful shutdown, or by any wait_for later wrapped above this --
        # would cancel that walk for the leader and for every other
        # follower attached to it, discarding hundreds of upstream requests
        # already paid for. shield() is the documented insulation for
        # exactly this, and leaves a departing follower costing nothing but
        # its own place in the queue. Cancellation still travels the other
        # way by design: if the leader's task is itself cancelled, shield
        # propagates that to every follower, which is correct -- there is no
        # longer a result coming for them to wait on.
        return await asyncio.shield(leader)

    task = asyncio.ensure_future(_walk_author_books(asin, region, session, deadline=deadline))
    _author_books_inflight[inflight_key] = task
    try:
        # Deliberately NOT shielded, unlike the follower await above, and
        # the asymmetry is the point rather than an oversight. _walk_author_books
        # runs on THIS request's session -- the one get_session's `async
        # with` closes the moment this coroutine unwinds. A shielded leader
        # would leave the walk querying that session afterwards, and that
        # does not fail loudly enough to notice: measured against this
        # engine, a session used after its context manager exited silently
        # checks a fresh connection out of the pool and autobegins on it,
        # with nothing left to check it back in and only SQLAlchemy's
        # garbage-collector warning as a trace. Every cancelled leader
        # would strand a connection from a pool shared with the seeder and
        # every background writer. So a cancelled leader takes its walk
        # with it; the followers lose it too, but nothing is left running
        # against a session no one owns. Surviving a leader's cancellation
        # would need the walk to hold a session of its own instead of
        # borrowing the caller's, which is a different change from this one.
        #
        # Keeping this await unshielded is also what keeps the eviction
        # below correct as written: the leader coroutine and the task end
        # together, so popping the key here can never leave a live walk
        # unreachable in the map, nor evict a key that still has a running
        # task behind it.
        return await task
    finally:
        # Cleared on every exit, success or exception, so a single failed
        # walk can never leave this key permanently uncoalescable -- a
        # failure here is a transient fetch or a NotFoundException, not a
        # reason to poison every future request for this author.
        _author_books_inflight.pop(inflight_key, None)


async def _walk_author_books(
    asin: str,
    region: str,
    session: AsyncSession,
    *,
    time_budget: float = AUTHOR_BOOKS_TIME_BUDGET_SECONDS,
    allow_background_completion: bool = True,
    deadline: float | None = None,
) -> AuthorBooksResult:
    """
    Fetches all book ASINs for an author, as a four-source parallel union:
    the Android author-detail screen (the only ASIN-exact source), and the
    catalog endpoint, windowed across unfiltered sorts and, for a large
    enough author, further sliced by category (see
    _fetch_author_books_by_catalog's own docstring for the full phase
    sequence). None of the four is complete or a subset of another --
    verified live against Agatha Christie (product_count 1116 on the
    screens grid, total_results 1138 on the catalog): the screens grid
    itself serves only 520 unique ASINs before plateauing; -ReleaseDate
    alone plateaus at 500 with zero overlap against ascending
    ReleaseDate's own, different 499; and 28 of the screens grid's 520
    ASINs never appear anywhere in the catalog union at all. Only the
    union of every source gets close to complete.

    Four waves:
      1. Resolve the author ASIN to a name via the contributors lookup --
         required because the catalog has no author-ASIN filter (probed
         live: author_asin, authorAsin, contributor_asin, and author_id
         are all silently ignored).
      2 & 3. The screens walk (_fetch_author_books_by_screen) and the
         catalog walk (_fetch_author_books_by_catalog) each run their own
         internal page-1/page-0-then-fan-out sequence, but the two
         top-level walks run concurrently with each other via
         asyncio.gather -- so screens' page 1 and the catalog's first
         windows go out together, and each source's own further rounds go
         out shortly after, without one source's walk blocking the
         other's. Neither walk applies its own concurrency bound beyond
         what's already built into it; the shared client throttles
         fan-out globally.
      4. Union everything: catalog ASINs first (already -ReleaseDate-
         first internally -- _fetch_author_books_by_catalog folds every
         window into its own result strictly one window at a time, in a
         fixed priority order, not merely in fetch order), then any
         screens-only extra, then any DB-only extra the two live sources
         didn't happen to surface this run.

    List order is a consumer-visible contract: consumers have always
    received the -ReleaseDate catalog list at the front of the response
    and must keep receiving that same list, in that same order, unmoved
    at the front of this union.
    Nothing in this path is sorted -- sort_dicts leaves order untouched
    when no sort param is passed, and both routes default to None, so
    this order reaches consumers directly.

    The DB's own record of this author's books is unioned in on every
    request, not only when a live source fails -- this is the
    less-data-never-accepted invariant applied at the response level: once
    Libex has seen a book for an author, it can never vanish from this
    response just because a source timed out or a sort window came back
    short this run.

    A source that fails (screens raises, or every catalog sort errors)
    does not fail the whole request -- whatever the other sources and the
    DB union surfaced is still served. Only when every source came back
    entirely empty, live and DB alike, is NotFoundException raised.
    """
    start = time.monotonic()
    # time_budget rather than the constant directly: the background
    # completion runs this same walk with a budget that is not bounded by
    # what a caller will sit and wait for, which is the whole reason it can
    # finish a walk a live request could not.
    # A caller-supplied deadline wins, so a live request bounds discovery and
    # hydration with ONE budget rather than two that add up. time_budget is
    # the fallback for callers that own no request clock -- the background
    # completion, which runs on a budget nobody is waiting on.
    if deadline is None:
        deadline = start + time_budget

    # Wave 1.
    author_name: str | None = None
    name_resolution_error: str | None = None
    try:
        author_name = await _resolve_author_name(asin, region, session)
    except Exception as e:
        name_resolution_error = f"{type(e).__name__}: {e}"

    # _resolve_author_name queries the DB (get_author_from_db) and, on the
    # name-write path, only ever hands off to a background task running
    # its own separate session (persist_author_background) -- nothing
    # past this point still needs session's transaction open. Releasing
    # it here, rather than leaving it checked out idle-in-transaction
    # across the network gather below (bounded by
    # AUTHOR_BOOKS_TIME_BUDGET_SECONDS) and the hydration after it,
    # returns the connection to a pool shared with the seeder and
    # background writers; SQLAlchemy re-acquires automatically the next
    # time session touches the DB (the wave 4 DB backstop read further
    # down).
    await session.rollback()

    # Waves 2 & 3: screens and catalog run concurrently as two top-level
    # tasks. Catalog is only scheduled at all when a name was resolved --
    # there is nothing to query the catalog with otherwise.
    #
    # author_books_concurrency() puts every audible_get call this gather
    # spawns -- both top-level tasks and every further nested gather each
    # of them runs internally (screens' own page fan-out, catalog's wave 2
    # and wave 3) -- on the wider AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT pool
    # instead of the default one every other Audible caller (single book/
    # author/series lookups, the seeder) still uses. See that pool's own
    # docstring in client.py for why this call path specifically gets it
    # and the measurements behind the chosen limit.
    with author_books_concurrency():
        tasks = [_fetch_author_books_by_screen(asin, region, deadline=deadline)]
        if author_name:
            tasks.append(
                _fetch_author_books_by_catalog(asin, author_name, region, deadline=deadline)
            )
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    screen_outcome = outcomes[0]
    screen_result: _ScreenBooksResult | None = None
    screen_error: str | None = None
    if isinstance(screen_outcome, BaseException):
        screen_error = f"{type(screen_outcome).__name__}: {screen_outcome}"
    else:
        screen_result = screen_outcome
    screen_asins = screen_result.asins if screen_result is not None else []

    catalog_result: _CatalogBooksResult | None = None
    catalog_error: str | None = None
    if author_name:
        catalog_outcome = outcomes[1]
        if isinstance(catalog_outcome, BaseException):
            catalog_error = f"{type(catalog_outcome).__name__}: {catalog_outcome}"
        else:
            catalog_result = catalog_outcome
    catalog_asins = catalog_result.asins if catalog_result is not None else []

    # catalog_error alone is essentially never set: _fetch_author_books_by_catalog
    # swallows every per-page and per-window failure into sort_errors /
    # truncated_by_deadline rather than raising, so an outright exception
    # from the gather is the rare case, not the common one. catalog_degraded
    # is the signal that actually reflects that -- it also covers those
    # swallowed failures and catalog_result.slicing_incomplete (the walk
    # decided it needed to slice by category but couldn't fully act on
    # that -- see _CatalogBooksResult's own docstring) -- but deliberately
    # does NOT treat catalog_result.sliced on its own as degraded: a
    # prolific author's baseline windows saturating their own
    # CATALOG_RESULT_CEILING and needing category slicing is the walk
    # working as designed, not a failure, and a walk that finished slicing
    # cleanly (dry-streak or ran out of ranked candidates, neither of
    # which sets sort_errors, truncated_by_deadline, or
    # slicing_incomplete) must read as complete -- treating a routine
    # ceiling as degraded here is exactly what previously forced every
    # prolific author to re-walk on every single request instead of
    # earning the default cache TTL once. Excludes the case where catalog
    # never ran because no author name was resolved (that is
    # name_resolution_error's concern, not catalog's).
    catalog_degraded = (
        author_name is not None
        and (
            catalog_error is not None
            or catalog_result is None
            or catalog_result.sort_errors
            or catalog_result.truncated_by_deadline
            or catalog_result.slicing_incomplete
        )
    )

    # Wave 4, part one: catalog first (already -ReleaseDate-first
    # internally), then any screens-only extra. Dedupe is ASIN-only, never
    # on title -- the screens grid alone holds 18 distinct ASINs of a
    # single title, each a real, separately trackable edition.
    seen = set(catalog_asins)
    asins = list(catalog_asins)
    for screen_asin in screen_asins:
        if screen_asin not in seen:
            seen.add(screen_asin)
            asins.append(screen_asin)

    # Wave 4, part two: the DB backstop, unioned on every request rather
    # than only when a live source came back short or empty (see
    # docstring). Canonicalized upper-case here to match the two live
    # producers above, since the reader returns ASINs as stored, not
    # normalized. get_author_book_asins_from_db returns None, distinct
    # from an empty list, when the read itself failed -- treated as empty
    # here for the union (there is nothing else to add), but db_clean /
    # db_error track the failure itself for the cache-write gate and the
    # logs below, so a failed DB read can never look identical to "this
    # author genuinely has no stored books" downstream.
    db_asins = await get_author_book_asins_from_db(session, asin, region)
    db_clean = db_asins is not None
    db_error = None if db_clean else "DB read for author book asins returned None"
    for db_asin in db_asins or []:
        db_asin = db_asin.upper()
        if db_asin not in seen:
            seen.add(db_asin)
            asins.append(db_asin)

    # Same defect class as the wave-1 rollback above, one phase later: the
    # get_author_book_asins_from_db read just above is this function's last
    # touch of session on the common (non-empty-union) path below, and
    # use_cache defaults False at the router -- get_books_by_asins then
    # never touches session at all during hydration. Left open, session
    # would sit checked out of the pool, idle-in-transaction, for the
    # entire hydration this function's return kicks off (measured: 84
    # chunks for a 4164-ASIN author, 10-15s+, plus normalizing every one of
    # those objects), pinning xmin cluster-wide against autovacuum for a
    # window idle_in_transaction_session_timeout (60s) never catches
    # because the hydration itself runs under that by design. Nothing below
    # this point still needs the transaction open; SQLAlchemy re-acquires
    # automatically the next time session touches the DB (the empty-union
    # cache.get fallback immediately below).
    await session.rollback()

    if not asins:
        cached = await cache.get(session, author_books_key(asin, region))
        # cache.get issues its own SELECT, autobeginning a fresh transaction
        # on session the same way the DB backstop read above did -- rolled
        # back here for the same reason, so this rare empty-union path
        # can't leave session idle-in-transaction through the rest of this
        # branch either, whether it returns the cache hit below or falls
        # through to raise NotFoundException.
        await session.rollback()
        if cached:
            # Complete by the same invariant get_author_books' own hit
            # relies on: only a finished walk is ever written to this key.
            return AuthorBooksResult(cached, True)

        logger.warning("Audible Author Books unavailable from every path", extra={
            "author_asin": asin,
            "region": region,
            "screen_error": screen_error,
            "screen_page_error": screen_result.page_error if screen_result else None,
            "catalog_error": catalog_error,
            "catalog_sort_errors": catalog_result.sort_errors if catalog_result else [],
            "name_resolution_error": name_resolution_error,
            "db_error": db_error,
        })
        if (
            screen_error is not None
            or catalog_degraded
            or name_resolution_error is not None
            or db_error is not None
        ):
            raise NotFoundException("Audible unavailable and no cached author books found")
        raise NotFoundException(f"No books found for author: {asin}")

    author_book_took = round((time.monotonic() - start) * 1000, 2)

    # Completeness signal, measured against this union's own len(asins) --
    # never against what a single source alone returned, since
    # product_count over-claims a single source's reach by more than 2x
    # (see _fetch_author_books_by_screen, where the same reasoning is why
    # no comparable per-source warning exists there either).
    # catalog_result.total_results is the preferred claim;
    # screen_result.product_count is used only when
    # catalog produced none at all (catalog errored, or its page 0 never
    # carried the field), since the two describe the same underlying
    # catalog size rather than independent counts to add together. The
    # union can legitimately exceed the claim -- screens and the DB
    # backstop both surface ASINs the catalog sorts never do -- so an
    # overshoot is never treated as a shortfall.
    claimed_total = catalog_result.total_results if catalog_result is not None else None
    if claimed_total is None and screen_result is not None:
        claimed_total = screen_result.product_count

    if claimed_total is None:
        logger.info("Audible Author Books completeness check skipped: no claimed total from catalog or screens", extra={
            "author_asin": asin,
            "region": region,
        })
    else:
        shortfall = claimed_total - len(asins)
        if shortfall > 0:
            shortfall_ratio = shortfall / claimed_total
            # Christie -- a real, healthy result -- sits 5 short of a 1138
            # claim, a 0.4% gap that must not warn or the signal is noise
            # again on day one. 10% clears that gap with wide margin while
            # still catching a result at half the claim, which obviously
            # must warn. Excluded when the catalog walk sliced by category
            # (catalog_result.sliced): a sliced walk's own claimed_total is
            # still just the unfiltered baseline's total_results, which a
            # deliberately-not-exhaustive category slice (stopped on a dry
            # streak once further exploration stopped paying off -- see
            # _fetch_author_books_by_catalog) is expected to land under
            # without that being a shortfall to warn about.
            if shortfall_ratio > 0.10 and not (catalog_result is not None and catalog_result.sliced):
                logger.warning("Audible Author Books union fell short of the claimed total", extra={
                    "author_asin": asin,
                    "region": region,
                    "author_book_num": len(asins),
                    "claimed_total": claimed_total,
                    "shortfall": shortfall,
                    "shortfall_ratio": round(shortfall_ratio, 4),
                    "screens_plateau_truncated": (
                        screen_result is not None
                        and screen_result.termination_reason == SCREENS_REASON_PLATEAU_TRUNCATED
                    ),
                })

    # less-data-never-accepted, applied through the TTL rather than a
    # cache-or-don't decision: persist_author_books_cache_background's write
    # is a union with whatever is already stored, taken under
    # SELECT ... FOR UPDATE (see its own docstring in db/persist_queue.py), so a
    # partial write can only ever grow the stored list, never shrink it --
    # there is no storage-side reason left to withhold this write on an
    # incomplete result. What still varies by completeness is the TTL.
    #
    # screens_clean asks the same question catalog_clean and db_clean ask
    # of their own waves below: did THIS wave fail its own job, not "did it
    # single-handedly finish a job it was never architecturally responsible
    # for finishing alone". The catalog wave runs unconditionally and the
    # DB backstop is unioned in on every request regardless of what screens
    # did -- screens plateauing is the designed handoff to those other two
    # sources, not evidence anything is missing. So this checks
    # SCREENS_BROKEN_REASONS (see screens.py, which also carries the
    # reasoning for every reason's inclusion or exclusion) rather than
    # requiring termination_reason == SCREENS_REASON_COMPLETED the way
    # SCREENS_CLEAN_REASONS does for that module's own, different, question
    # ("did upstream itself confirm nothing remains") -- SCREENS_REASON_
    # PLATEAU_TRUNCATED is the one reason that's unclean by that stricter
    # question but not broken by this one: Christie's screens grid plateaus
    # at page 26 of a product_count-implied 56 and still contributes
    # roughly half of her 1133-ASIN union, and gating completeness on
    # COMPLETED alone made is_complete permanently unreachable for exactly
    # the prolific authors this feature exists to serve, forcing them onto
    # the not-cached-at-all path forever instead of the default day-long
    # TTL -- a full walk on every single request, each hundreds of upstream
    # requests, for exactly the authors that cost the most to walk.
    #
    # catalog_clean is True either when catalog ran with no page-fetch
    # errors, wasn't cut short by the deadline, and wasn't left unable to
    # finish slicing what it identified as worth slicing
    # (catalog_result.slicing_incomplete), or when author_name is a
    # confirmed absence: _resolve_author_name returned None without
    # raising, meaning Audible itself reported this author has no name, so
    # the catalog wave has nothing to search and that case is trivially
    # complete, not degraded. catalog_result.sliced is deliberately NOT
    # part of this gate -- a walk that sliced by category and then stopped
    # cleanly, on a dry streak or by running out of ranked candidates (see
    # _fetch_author_books_by_catalog's own docstring), is exactly this
    # feature completing as designed and must earn the same default TTL a
    # small author's single-baseline-pass walk does; gating on "did it
    # need to slice at all" is the trap that would turn every prolific
    # author's request into a re-walk forever, which is strictly worse
    # than not slicing at all. author_name
    # is also None when name resolution raised instead of confirming an
    # absence -- name_resolution_error is checked explicitly here to keep
    # the two apart, since that case means the catalog wave never got a
    # chance to run at all and must not read as trivially complete.
    # db_clean is False when the DB backstop read itself failed (returned
    # None, distinct from a genuinely empty list) -- a failed DB read means
    # the union never got the chance to include whatever it already had
    # stored, so this run is not confirmed complete either. When all three
    # hold, the result is written with the default TTL (settings.cache_ttl);
    # otherwise it is not written at all and is handed to background
    # completion instead -- see the branch below for why storing a partial
    # stopped being acceptable once the cache began serving the default
    # path.
    screens_clean = (
        screen_result is not None
        and screen_result.termination_reason not in SCREENS_BROKEN_REASONS
    )
    catalog_clean = (
        (author_name is None and name_resolution_error is None)
        or (
            catalog_result is not None
            and catalog_error is None
            and not catalog_result.sort_errors
            and not catalog_result.truncated_by_deadline
            and not catalog_result.slicing_incomplete
        )
    )
    is_complete = screens_clean and catalog_clean and db_clean
    if is_complete:
        persist_author_books_cache_background(author_books_key(asin, region), asins, ttl_seconds=None)
    elif allow_background_completion:
        # An unfinished walk is NOT written here, and that is a reversal of
        # what this did before. It used to be stored under a short
        # fifteen-minute TTL, on the reasoning that a
        # prolific author whose walk can never finish clean would otherwise
        # never be cached and would re-walk on every request. That reasoning
        # was sound while the cache served only callers who passed
        # ?cache=true. It stopped being sound when the cache began serving
        # the default path: storing a partial there means handing it to
        # everyone, for the whole TTL, as though it were the answer.
        #
        # So the invariant this establishes is that ANY hit on this key is a
        # complete result. That is what makes a cached answer safe to serve
        # without qualification, and it is the precondition for letting an
        # edge cache hold one at all.
        #
        # The old reasoning is answered instead by finishing the job rather
        # than by lowering the standard: request_author_books_completion
        # re-runs this walk in the background, off the request, with a
        # budget that is not bounded by what a caller will wait for, and
        # caches THAT. The caller still gets this partial list now -- it is
        # returned below and the route marks the response incomplete -- it
        # just does not become what the next caller is told is the truth.
        request_author_books_completion(asin, region, asins)
    if catalog_result is not None and catalog_result.slicing_incomplete:
        # A distinct, measured signal -- the walk identified categories
        # worth slicing by (its baseline plateaued or over-claimed
        # total_results) but found literally nothing to slice with, which
        # for an author this large points at something broken upstream
        # (see _CatalogBooksResult's own docstring for why hitting
        # CATALOG_MAX_CANDIDATE_CATEGORIES alone does NOT set this) -- so
        # it's called out on its own, separate from the generic
        # degraded-path warning below, which only fires on an outright
        # error. The write itself still happens (see above); this only
        # reports why it got the short TTL instead of the default one.
        logger.warning("Audible Author Books catalog could not finish slicing by category, cached with a short TTL", extra={
            "author_asin": asin,
            "region": region,
            "catalog_total_results": catalog_result.total_results,
            "catalog_categories_harvested": catalog_result.categories_harvested,
            "catalog_categories_considered": catalog_result.categories_considered,
            "catalog_categories_expanded": catalog_result.categories_expanded,
            "author_book_num": len(asins),
        })

    # A path that errored still counts as a degraded response even when the
    # union it contributed to came back non-empty -- folding these into the
    # INFO line below as bare fields would let that degradation stay under
    # the WARNING threshold and never reach a WARNING-level alert or
    # stderr. catalog_degraded stands in for a bare catalog_error is not
    # None check here (see where it's computed above): the raw exception
    # case is rare, the swallowed-into-sort_errors/truncated_by_deadline
    # case is the common one, and only catalog_degraded catches both.
    if (
        screen_error is not None
        or catalog_degraded
        or name_resolution_error is not None
        or db_error is not None
    ):
        logger.warning("Audible Author Books served from a degraded path", extra={
            "author_asin": asin,
            "region": region,
            "screen_error": screen_error,
            "screen_page_error": screen_result.page_error if screen_result else None,
            "catalog_error": catalog_error,
            "catalog_sort_errors": catalog_result.sort_errors if catalog_result else [],
            "name_resolution_error": name_resolution_error,
            "db_error": db_error,
        })

    logger.info("Requested Audible Author Books", extra={
        "author_asin": asin,
        "author_name": author_name,
        "author_book_num": len(asins),
        "screen_asin_num": len(screen_asins),
        "catalog_asin_num": len(catalog_asins),
        "screen_pages_fetched": screen_result.pages_fetched if screen_result else 0,
        "screen_product_count": screen_result.product_count if screen_result else None,
        "screen_invalid_skipped": screen_result.invalid_skipped if screen_result else 0,
        "screen_attribution_rejected": screen_result.attribution_rejected if screen_result else 0,
        "screen_sections_truncated": screen_result.sections_truncated if screen_result else 0,
        "screen_rows_truncated": screen_result.rows_truncated if screen_result else 0,
        "screen_termination_reason": screen_result.termination_reason if screen_result else None,
        "screen_error": screen_error,
        "screen_page_error": screen_result.page_error if screen_result else None,
        "catalog_pages_fetched": catalog_result.pages_fetched if catalog_result else 0,
        "catalog_total_results": catalog_result.total_results if catalog_result else None,
        "catalog_windows_used": catalog_result.windows_used if catalog_result else 0,
        "catalog_sliced": catalog_result.sliced if catalog_result else False,
        "catalog_categories_harvested": catalog_result.categories_harvested if catalog_result else 0,
        "catalog_categories_considered": catalog_result.categories_considered if catalog_result else 0,
        "catalog_categories_expanded": catalog_result.categories_expanded if catalog_result else 0,
        "catalog_categories_complete_after_probe": (
            catalog_result.categories_complete_after_probe if catalog_result else 0
        ),
        "catalog_slicing_incomplete": catalog_result.slicing_incomplete if catalog_result else False,
        "catalog_asin_match": catalog_result.asin_match_count if catalog_result else 0,
        "catalog_asin_reject": catalog_result.asin_reject_count if catalog_result else 0,
        "catalog_name_match": catalog_result.name_match_count if catalog_result else 0,
        "catalog_name_reject": catalog_result.name_reject_count if catalog_result else 0,
        "catalog_truncated_by_deadline": catalog_result.truncated_by_deadline if catalog_result else False,
        "catalog_sort_errors": catalog_result.sort_errors if catalog_result else [],
        "catalog_error": catalog_error,
        "name_resolution_error": name_resolution_error,
        "db_error": db_error,
        "author_book_took": author_book_took,
        "region": region,
    })

    return AuthorBooksResult(asins, is_complete)


async def get_author_books_by_name(
    name: str,
    region: str,
    session: AsyncSession,
) -> list[str]:
    """Fetches book ASINs by author name."""
    try:
        start = time.monotonic()
        asins, pages_fetched = await fetch_author_books_by_name(name, region)
        author_book_took = round((time.monotonic() - start) * 1000, 2)

        if not asins:
            raise NotFoundException(f"No books found for author name: {name}")

        # Deliberately no "author_name" field: on this route the name is
        # verbatim caller-authored text, not catalogue data resolved from an
        # ASIN, and Libex records nothing that identifies a caller and nothing
        # a caller typed. Nothing stands in for it -- unlike a search query,
        # whose length correlates with how it performed, an author name's
        # length explains nothing the page count below does not already.
        logger.info("Requested Audible Author Books By Name", extra={
            "author_book_num": len(asins),
            "pages_fetched": pages_fetched,
            "author_book_took": author_book_took,
            "region": region,
        })

        return asins

    except NotFoundException:
        raise
    except Exception:
        raise NotFoundException("Failed to fetch author books by name")


async def search_authors(
    name: str,
    region: str,
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Searches for authors by name using Audible search suggestions."""
    try:
        start = time.monotonic()
        path = "/1.0/searchsuggestions"
        params = {
            "keywords": name,
            "key_strokes": name,
            "site_variant": "android-mshop",
            "session_id": _generate_session_id(),
            "local_time": datetime.utcnow().isoformat(),
            "surface": "Android",
        }

        data = await audible_get(region, path, params)
        search_took = round((time.monotonic() - start) * 1000, 2)

        asins: list[str] = []
        for item in data.get("model", {}).get("items", []):
            if item.get("view", {}).get("template") == "AuthorItemV2":
                asin = item.get("model", {}).get("person_metadata", {}).get("asin")
                if asin:
                    asins.append(asin)

        logger.info("Requested Audible Author Search", extra={
            "search_took": search_took,
            "region": region,
        })

        if not asins:
            return []

        authors = []
        for asin in asins:
            try:
                author = await get_author(asin, region, session)
                authors.append(author)
            except NotFoundException:
                continue

        return authors

    except NotFoundException:
        raise
    except Exception:
        raise NotFoundException("Author search failed")