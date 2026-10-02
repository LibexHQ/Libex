"""
Audible books service.
Fetches book metadata directly from the Audible API.

DESIGN PHILOSOPHY: Audible-first.
Audible is the source of truth, and every result normalized from it is
written to the relational DB for persistence.

One cohesive job lives here: turning a list of ASINs into books, by whatever
mix of Audible, the relational DB, and the cache it takes to answer without
handing back less than a caller could have gotten a moment ago. Asking
Audible and normalizing what it sends back is libex_core.audible.books; what
stays here is the fallback ladder in get_books_by_asins, which hands back DB
rows and cache hits alongside freshly normalized Audible products in the very
same list, and the tri-state flag settling that every return path goes
through, which has to treat whichever of the three produced a given element
identically. That ladder is one job, and a seam drawn through it would
stretch a shared contract across two files rather than make a boundary.

A second cluster shares the module without sharing that job: get_chapters,
fetch_and_store_chapters, and _mark_chapters_checked fetch a book's chapter
listing from Audible (see libex_core.audible.chapters) and persist it to its
own table (Track) under its own cache key -- never touching
get_books_by_asins' fallback ladder, its DB backstop, its facts ledger, or
the tri-state flag settling above. The one real tie to the rest of the module
is _mark_chapters_checked stamping chapters_checked_at on the same Book row
the ladder resolves, coordinating with the standalone chapters backfill so
neither re-checks what the other already covered; past that one column, the
cluster is its own job, sharing an ASIN and a file with the ladder above
rather than a boundary. The module stays long because it holds one long job
and one short one that happens to touch the same row.

Every request to Audible goes through audible_get, read from this module's
namespace at the moment of each call rather than bound once at import, so
swapping it for an instrumented stand-in reaches every fetch made here.
"""

# Standard library
import asyncio
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any

# Third party
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.models import Book

# Core
from libex_core.audible.books import (
    MAX_ASINS_PER_REQUEST,
    fetch_products,
    filter_products,
    is_placeholder_record,
    normalize_products,
    settle_flags_list,
)
from libex_core.audible.chapters import (
    fetch_chapter_metadata,
    has_chapter_info,
    normalize_chapters,
)
from libex_core.audible.client import (
    as_audible_failure,
    author_books_concurrency,
    upstream_status_of,
)
from libex_core.exceptions import NotFoundException
from app.core.logging import get_logger
from app.core.response_headers import (
    REASON_HYDRATION_DEADLINE,
    REASON_HYDRATION_FAILED,
    REASON_HYDRATION_NOT_FOUND,
    ResponseFacts,
    SOURCE_AUDIBLE,
    SOURCE_CACHE,
    SOURCE_DB,
    record_incomplete,
    record_source,
    record_source_keys,
)

# Services
from app.services.audible import audible_get
from app.services.cache import manager as cache
from app.services.cache.manager import book_key, chapters_key
from app.services.db.persist_queue import (
    PersistOutcome,
    persist_books_background,
    persist_track_background,
)
from app.services.db.writer import upsert_track
from app.services.db.reader import get_books_from_db, get_track_from_db

logger = get_logger()

# ============================================================
# HELPERS
# ============================================================

def _has_uncovered(asins: list[str], covered: set[str]) -> bool:
    """
    True when at least one of `asins` is absent from `covered`.

    The one predicate every hydration-incomplete site in
    _get_books_by_asins_unsettled shares: a loss class earns its incomplete
    reason only when it actually owns an ASIN missing from what the function
    is about to hand back, never merely because that class experienced a
    loss internally. `covered` is always the ASIN set already present in the
    result about to be returned (a DB backstop, a DB fallback, a cache
    fallback) -- never a count, since two same-sized sets can still miss
    each other entirely.
    """
    return any(asin not in covered for asin in asins)


# ============================================================
# CHUNKING
# ============================================================

async def _await_chunks(tasks, timeout, chunks, region) -> None:
    """Waits out the chunk fan-out, cancelling whatever is still in flight
    when the request's deadline arrives.

    Split from the caller so the try/finally that guarantees cancellation on
    an OUTER cancel stays one readable statement -- see that finally for why
    it has to exist at all.
    """
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        # Let the cancellations settle before anything reads a task, so
        # nothing is still running when results are assembled.
        await asyncio.gather(*pending, return_exceptions=True)
        logger.warning("Hydration deadline reached, chunks abandoned", extra={
            "abandoned_chunks": len(pending),
            "total_chunks": len(chunks),
            "region": region,
        })


class HydrationDeadlineExceeded(Exception):
    """One hydration chunk abandoned because the request ran out of time.

    Deliberately an Exception rather than letting CancelledError through:
    CancelledError is a BaseException in 3.12, so it would slip past the
    `isinstance(result, Exception)` branch below that routes a failed chunk to
    the DB backstop. Abandoned chunks must take that path -- their ASINs are
    still worth answering from stored rows -- and must also leave the response
    visibly short, which is what makes the route mark it incomplete.
    """


async def _fetch_chunk(asins: list[str], region: str) -> list[dict[str, Any]]:
    """Fetches a single chunk of up to 50 ASINs from Audible.

    Returns Audible's products raw. Filtering is the caller's job, because
    only the caller can tell a placeholder from a stub among the drops.

    audible_get is looked up here, in this module's namespace, on every call
    -- never captured into a default argument, a partial, or an import-time
    local -- so a stand-in assigned over it is the one that gets called.
    """
    return await fetch_products(audible_get, asins, region)


# ============================================================
# PUBLIC API
# ============================================================

async def get_books_by_asins(
    asins: list[str],
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
    high_concurrency: bool = False,
    deadline: float | None = None,
    *,
    facts: ResponseFacts | None = None,
    persist_outcome: list[PersistOutcome] | None = None,
    placeholder_asins: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Public entry point. Delegates to _get_books_by_asins_unsettled and settles
    the seven tri-state flags (see settle_flags) on whatever it returns
    before handing it back.

    That inner function has several return points -- an early cache hit, the
    full-fetch success path, and two different failure fallbacks -- and every
    one of them can carry a None flag, whether freshly normalized or read back
    from a cache entry someone else wrote with the tri-state still in it.
    Settling once here, on the outside, covers all of them from a single place
    and can't be silently bypassed by a return path added inside later; the
    alternative was inserting the same call at each of those points.

    facts is threaded straight through, unexamined -- it is
    _get_books_by_asins_unsettled's own return points, not this wrapper's
    settling step, that know which source produced which element.

    persist_outcome is the same out-parameter idiom as facts, kept as its own
    plain list rather than folded into ResponseFacts: whether the background
    write queue admitted or shed this call's books is an internal storage
    fact a caller like the seeder needs to gate a retry decision on, not a
    caller-facing hydration fact the way ResponseFacts' tally and incomplete-
    reason set are -- those feed X-Libex-Incomplete-Reason, which persistence
    shedding has nothing to do with. None (the default) costs every existing
    caller nothing, the same as facts=None. When given, gets at most one
    PersistOutcome appended -- this function's own fetch path calls
    persist_books_background at most once per invocation.

    placeholder_asins, when given, receives the requested ASINs Audible
    answered with a placeholder record (see is_placeholder_record). They are
    absent from the result like any unresolved ASIN; this is how a caller
    tells "Audible has no such book" from "Audible has it and won't serve it".
    Only a chunk Audible answered can classify, and its placeholders are
    listed even if another chunk fails and the request falls back to stored
    copies. An ASIN in a failed or abandoned chunk is never classified,
    whether or not other chunks succeeded; with no stored copy covering it,
    it ends up in notFound. If no chunk produced a servable book and nothing
    came from the cache, a failed chunk sends the whole request to the outage
    fallback, which checks every requested ASIN against the database and then
    the cache; if that finds none of them, the route turns the outage into a
    whole-request 404 and no placeholder reaches the caller, even one from a
    chunk Audible answered. When any chunk did produce a book, an uncovered
    failed chunk only adds its ASINs to notFound. None leaves every other
    caller unchanged.
    """
    books = await _get_books_by_asins_unsettled(
        asins,
        region,
        session,
        use_cache,
        high_concurrency,
        deadline,
        facts=facts,
        persist_outcome=persist_outcome,
        placeholder_asins=placeholder_asins,
    )
    return settle_flags_list(books)


async def _get_books_by_asins_unsettled(
    asins: list[str],
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
    high_concurrency: bool = False,
    deadline: float | None = None,
    *,
    facts: ResponseFacts | None = None,
    persist_outcome: list[PersistOutcome] | None = None,
    placeholder_asins: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Fetches one or more books by ASIN from Audible.
    Writes results to relational DB and cache.
    Falls back to DB then cache when Audible is unavailable.

    high_concurrency, when True, runs the chunk fan-out below inside
    author_books_concurrency() (see client.py), drawing from the wider
    AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT pool instead of the default one.
    Set only by the author-ASIN routes hydrating get_author_books' own
    result -- the one path where a single request legitimately fans out to
    dozens of chunk requests and a live, measured production outage
    (5 concurrent author lookups 504ing at the fronting proxy's 30s timeout)
    traced directly back to that fan-out being serialized behind the
    default pool. Every other caller (single/small ASIN lists from the book,
    series, and search routes, and the seeder) defaults to False and is
    unaffected.

    facts, when given, is credited at every return point below with exactly
    which source each returned element came from -- this is the one function
    in the module where a single response can genuinely mix cache, fresh
    Audible, and DB-backstop elements, so the tally is built at each
    source-segmented concatenation rather than inferred afterwards from the
    merged list, which by that point no longer carries where any one element
    came from.

    persist_outcome, when given, records whether the background write queue
    admitted or shed the books freshly fetched from Audible in this call --
    see get_books_by_asins for why this rides its own list rather than
    ResponseFacts. Populated only where persist_books_background is actually
    called below (the all_products branch of the main success path); every
    other return -- an all-cache-hit early return, a not-found-only result,
    the Audible-unavailable fallback -- has nothing freshly fetched to
    persist and leaves the list untouched, exactly as it would if the caller
    hadn't asked.

    placeholder_asins, when given, is extended with the requested ASINs whose
    Audible product is a placeholder record, on both the batch and the
    single-ASIN fetch. Placeholders are never normalized, persisted or
    cached, and still count as incomplete. The list is filled from every chunk
    Audible answered, before any fallback is decided, so those placeholders
    stay listed even when another chunk failed and the outage fallback runs.
    An ASIN in a failed or abandoned chunk is never classified, whatever the
    other chunks did. When no chunk produced a servable book and nothing came
    from the cache, any failed chunk sends the request to the outage
    fallback; if no stored or cached copy of any requested ASIN exists
    either, it ends as a route-level 404, so the list is then never seen by
    the caller, placeholders from answered chunks included.
    """
    if not asins:
        raise NotFoundException("No ASINs provided")

    seen: set[str] = set()
    unique_asins = [a for a in asins if not (a in seen or seen.add(a))]  # type: ignore

    if use_cache and len(unique_asins) == 1:
        cached = await cache.get(session, book_key(unique_asins[0], region))
        if cached:
            record_source_keys(facts, SOURCE_CACHE, [b["asin"] for b in [cached]])
            return [cached]

    # Batch cache: one lookup for the whole list, only fetch misses from
    # Audible. Reading the keys back in unique_asins order keeps cached_results
    # in the caller's order: that is the entire response on the all-hits early
    # return below, and its leading segment on every other return, each of
    # which concatenates it ahead of the freshly-fetched and backstop results
    # rather than reordering it.
    cached_results: list[dict[str, Any]] = []
    fetch_asins = unique_asins
    if use_cache and len(unique_asins) > 1:
        keys = [book_key(a, region) for a in unique_asins]
        hits = await cache.get_many(session, keys)
        cached_results = []
        fetch_asins = []
        for asin, key in zip(unique_asins, keys):
            hit = hits.get(key)
            if hit:
                cached_results.append(hit)
            else:
                fetch_asins.append(asin)

        # All ASINs found in cache
        if not fetch_asins:
            record_source_keys(facts, SOURCE_CACHE, [b["asin"] for b in cached_results])
            return cached_results

    # A connection is held for work, not for a request. Either cache read
    # above autobegins a transaction on session, and a READ COMMITTED
    # transaction advertises backend_xmin and can become the cluster's oldest
    # xmin even when it has only ever read -- measured on PostgreSQL 16.14 --
    # holding the xmin horizon against autovacuum until it ends. Held across
    # the fan-out below, that window is whatever Audible takes: the chunk
    # requests are unbounded in time and queue against the process-wide pool
    # in client.py, so a bulk request at the route's 1000-ASIN cap is 20
    # chunks draining through a handful of permits in successive waves, with
    # nothing between the cache read and the last chunk that needs a
    # transaction open. Released here, before any of it.
    #
    # One placement covers every path into the fan-out. With use_cache False
    # neither read ran, session is still lazy and holds no connection, and
    # rollback with no transaction in progress is a pass-through that touches
    # neither the session nor the pool. Nothing after this point depends on
    # transaction state established before it: both reads return plain
    # already-materialized values into cached_results, and the two later
    # session users -- the DB backstop for transiently failed chunks, and the
    # outage fallback in the except branch -- each open their own transaction
    # on their next statement, which SQLAlchemy re-acquires transparently.
    await session.rollback()

    try:
        start = time.monotonic()
        chunks = [
            fetch_asins[i:i + MAX_ASINS_PER_REQUEST]
            for i in range(0, len(fetch_asins), MAX_ASINS_PER_REQUEST)
        ]

        # Fire every chunk concurrently -- audible_get itself is the throttle
        # point (a process-wide bound lives there), so nothing here needs to
        # cap fan-out. return_exceptions=True so a bad chunk can't wipe out
        # the chunks that already came back; gather still guarantees results
        # line up with chunks by index regardless of completion order, so
        # reassembly below stays in the same order the caller passed in.
        #
        # nullcontext when high_concurrency is False keeps every other
        # caller's behavior byte-identical to before this parameter existed
        # -- the pool draw only changes for the one path that opts in.
        pool_context = author_books_concurrency() if high_concurrency else nullcontext()
        with pool_context:
            # asyncio.wait with a timeout rather than gather, so the request's
            # own budget bounds hydration as well as discovery. Hydration used
            # to run entirely outside that budget: the walk stopped at its
            # deadline and then handed an unbounded fan-out to a caller the
            # proxy was already timing out on, so the worst case was the
            # discovery budget PLUS however long the books took.
            #
            # wait, not wait_for: wait_for cancels the whole gather and throws
            # away every chunk that had already come back. Here the chunks
            # that landed are kept, the ones still in flight are cancelled,
            # and their ASINs fall through to the DB backstop below exactly
            # as a transiently failed chunk does. The response is then
            # visibly short, which is what makes the route mark it incomplete
            # rather than advertising a half-hydrated body as whole.
            tasks = [
                asyncio.ensure_future(_fetch_chunk(chunk, region))
                for chunk in chunks
            ]
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                await _await_chunks(tasks, timeout, chunks, region)
            finally:
                # gather cancelled its children when the coroutine awaiting it
                # was cancelled; wait does not, and swapping one for the other
                # silently dropped that. Without this, an outer cancellation --
                # a graceful shutdown, or anything that later wraps these routes
                # in wait_for -- unwinds straight out of the await above and
                # leaves the whole fan-out running detached: no one holding the
                # tasks, each still holding an Audible pool permit and an httpx
                # connection, and any exception they raise never retrieved.
                # Discovery's own fan-out still uses gather and still gets this
                # for free; hydration has to ask for it.
                for task in tasks:
                    if not task.done():
                        task.cancel()
            # Rebuilt in the original chunk order, which zip() below relies on.
            results: list[Any] = []
            for task in tasks:
                if task.cancelled():
                    # Not recorded here: a cancelled chunk's ASINs still have
                    # the DB backstop below to answer from, and only the
                    # residue that backstop can't cover is an actual gap in
                    # what this function returns.
                    results.append(HydrationDeadlineExceeded())
                elif task.exception() is not None:
                    results.append(task.exception())
                else:
                    results.append(task.result())

        requested_took = round((time.monotonic() - start) * 1000, 2)

        all_products: list[dict[str, Any]] = []
        not_found_asins: list[str] = []
        deadline_asins: list[str] = []
        transient_failed_asins: list[str] = []
        placeholders: list[str] = []
        transient_errors: list[Exception] = []

        for idx, (chunk, result) in enumerate(zip(chunks, results)):
            if isinstance(result, NotFoundException):
                # Only the single-ASIN branch of _fetch_chunk can raise this
                # at all -- the batch endpoint's own response is always a 200
                # with a products array, even when every requested ASIN is
                # unknown, so a batch chunk never surfaces as an exception
                # here. A nonexistent-but-well-formed ASIN doesn't reach this
                # branch either way: probed live, Audible returns 200 with a
                # hollow, titleless stub for one of those in both branches,
                # and filter_products (see that function) is what turns that
                # stub into nothing to add rather than a 404. Whatever does
                # reach this branch is terminal for that one ASIN regardless,
                # not a reason to discard everything else that already
                # succeeded.
                not_found_asins.extend(chunk)
                record_incomplete(facts, REASON_HYDRATION_NOT_FOUND)
                continue
            if isinstance(result, Exception):
                transient_failed_asins.extend(chunk)
                transient_errors.append(result)
                if isinstance(result, HydrationDeadlineExceeded):
                    deadline_asins.extend(chunk)
                logger.warning(
                    "Hydration chunk failed",
                    extra={
                        "chunk_index": idx + 1,
                        "chunk_count": len(chunks),
                        "chunk_size": len(chunk),
                        "region": region,
                        "error_type": type(result).__name__,
                        "error": str(result),
                    },
                )
                continue
            # A batch answers 200 even when some requested ASINs have no
            # record: those come back as hollow titleless stubs, which
            # filter_products drops. A titled record with the placeholder
            # date is dropped too but is a different fact, so it is split
            # out. Only ASINs that were requested count; Audible can return
            # others.
            kept = filter_products(result)
            kept_asins = {p.get("asin") for p in kept}
            chunk_set = set(chunk)
            chunk_placeholders = list(dict.fromkeys(
                p["asin"] for p in result
                if p.get("asin") in chunk_set and is_placeholder_record(p)
            ))
            placeholder_set = set(chunk_placeholders)
            stub_asins = [
                a for a in chunk if a not in kept_asins and a not in placeholder_set
            ]
            if stub_asins:
                not_found_asins.extend(stub_asins)
            if chunk_placeholders:
                placeholders.extend(chunk_placeholders)
            if stub_asins or chunk_placeholders:
                record_incomplete(facts, REASON_HYDRATION_NOT_FOUND)
            all_products.extend(kept)

        if placeholder_asins is not None:
            placeholder_asins.extend(placeholders)

        if not_found_asins or placeholders or transient_failed_asins:
            # Like its siblings, the placeholder_asins log field is a count.
            logger.warning("Partial hydration shortfall", extra={
                "requested_num": len(fetch_asins),
                "not_found_asins": len(not_found_asins),
                "placeholder_asins": len(placeholders),
                "failed_asins": len(transient_failed_asins),
                "region": region,
            })

        # Every chunk that mattered failed transiently and nothing else came
        # back -- fall through to the DB/cache fallback below exactly as a
        # single sequential failure would have. A pure not-found (no transient
        # errors) does NOT take this path: 404 is terminal, not a retry signal.
        if transient_failed_asins and not all_products and not cached_results:
            raise transient_errors[0]

        if not all_products and not cached_results:
            return []

        # less-data-never-accepted for a partial transient failure: some
        # chunks succeeding (or use_cache already producing cache hits) used
        # to skip the DB backstop entirely for transient_failed_asins,
        # silently omitting whatever was already stored for exactly the
        # ASINs the failed chunk(s) covered -- a 1500-ASIN author plus one
        # upstream 503 dropped the ~50 stored books that chunk owned, and
        # use_cache=True with every chunk failing returned cache hits only.
        # Scoped to transient_failed_asins alone, never not_found_asins: a
        # 404 is a confirmed absence, not a retry signal, and must not be
        # papered over by stale DB data. Runs whenever any chunk failed
        # transiently, independent of whether all_products or cached_results
        # already have something, so neither can silently swallow it the way
        # both used to.
        db_backstop_results: list[dict[str, Any]] = []
        if transient_failed_asins:
            db_backstop_results = await get_books_from_db(session, transient_failed_asins)
            # The backstop covers what the DB actually had; anything still
            # missing after it is a real shortfall the caller has to be told
            # about, not just an internal retry detail -- and which reason it
            # carries follows the ASIN, not the query: a deadline chunk the
            # backstop couldn't cover is still a deadline loss, not a generic
            # hydration failure, even though both walked the same query.
            backstop_asins = {b["asin"] for b in db_backstop_results}
            deadline_set = set(deadline_asins)
            other_failed_asins = [a for a in transient_failed_asins if a not in deadline_set]
            if _has_uncovered(deadline_asins, backstop_asins):
                record_incomplete(facts, REASON_HYDRATION_DEADLINE)
            if _has_uncovered(other_failed_asins, backstop_asins):
                record_incomplete(facts, REASON_HYDRATION_FAILED)

        normalized = await normalize_products(all_products, region)

        if all_products:
            # Persist to DB and cache in the background
            outcome = persist_books_background(normalized, region)
            if persist_outcome is not None:
                persist_outcome.append(outcome)

        logger.info("Requested books from Audible", extra={
            "requested_num": len(fetch_asins),
            "cache_hits": len(cached_results),
            "requested_took": requested_took,
            "not_found_asins": len(not_found_asins),
            "placeholder_asins": len(placeholders),
            "failed_asins": len(transient_failed_asins),
            "db_backstop_num": len(db_backstop_results),
            "region": region,
        })

        record_source_keys(facts, SOURCE_CACHE, [b["asin"] for b in cached_results])
        record_source_keys(facts, SOURCE_AUDIBLE, [b["asin"] for b in normalized])
        record_source_keys(facts, SOURCE_DB, [b["asin"] for b in db_backstop_results])
        return cached_results + normalized + db_backstop_results

    except NotFoundException:
        raise

    except Exception as e:
        await session.rollback()
        logger.warning(
            "Audible unavailable, attempting DB fallback",
            extra={"asins": fetch_asins},
        )

        # Try relational DB first for the misses
        db_results = await get_books_from_db(session, fetch_asins)
        if db_results:
            record_source_keys(facts, SOURCE_CACHE, [b["asin"] for b in cached_results])
            record_source_keys(facts, SOURCE_DB, [b["asin"] for b in db_results])
            db_asins = {b["asin"] for b in db_results}
            if _has_uncovered(fetch_asins, db_asins):
                record_incomplete(facts, REASON_HYDRATION_FAILED)
            return cached_results + db_results

        # Fall back to cache for the misses -- one lookup, same as the
        # pre-fetch check above, and read back in fetch_asins order.
        fallback_results = []
        fallback_keys = [book_key(a, region) for a in fetch_asins]
        fallback_hits = await cache.get_many(session, fallback_keys)
        for key in fallback_keys:
            hit = fallback_hits.get(key)
            if hit:
                fallback_results.append(hit)
        if fallback_results or cached_results:
            # Both segments are cache reads -- cached_results from the
            # pre-fetch batch lookup, fallback_results from this outage
            # fallback's own -- so they're one source, one token, added
            # together rather than as two separate calls.
            record_source_keys(
                facts,
                SOURCE_CACHE,
                [b["asin"] for b in cached_results] + [b["asin"] for b in fallback_results],
            )
            # fetch_asins is what this fallback owed an answer for -- always
            # non-empty here, since an empty one would have returned via the
            # all-cache-hits path above before any of this outage handling
            # ran. cached_results predates fetch_asins by construction and
            # never overlaps it, so the coverage test reads only against
            # fallback_results, not the two summed.
            fallback_asins = {b["asin"] for b in fallback_results}
            if _has_uncovered(fetch_asins, fallback_asins):
                record_incomplete(facts, REASON_HYDRATION_FAILED)
            return cached_results + fallback_results

        # Neither a stored copy nor a cached one exists -- that is silence,
        # not a confirmed absence, so what reaches the caller has to say
        # Audible could not be reached rather than that these books are not
        # there.
        raise as_audible_failure(
            e, "Audible unavailable and no cached data found"
        ) from e


async def get_book_by_asin(
    asin: str,
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
    *,
    facts: ResponseFacts | None = None,
) -> dict[str, Any]:
    """Fetches a single book by ASIN."""
    books = await get_books_by_asins([asin], region, session, use_cache, facts=facts)
    if not books:
        raise NotFoundException(f"Book not found: {asin}")
    return books[0]


async def get_chapters(
    asin: str,
    region: str,
    session: AsyncSession,
    *,
    facts: ResponseFacts | None = None,
) -> dict[str, Any]:
    """
    Fetches chapter information for a book by ASIN.
    Returns data matching AudiMeta's TrackContentDto format.

    Single-source by construction -- audible, then db, then cache, never more
    than one per call -- so facts takes exactly one record_source per return
    path rather than the per-element tally _get_books_by_asins_unsettled
    needs.
    """
    try:
        start = time.monotonic()
        data = await fetch_chapter_metadata(audible_get, asin, region)
        chapters_took = round((time.monotonic() - start) * 1000, 2)

        if not has_chapter_info(data):
            raise NotFoundException(f"No chapter information found for {asin}")

        result = normalize_chapters(data)

        # Persist to DB and cache in the background
        persist_track_background(asin, result, region)

        logger.info("Requested chapters from Audible", extra={
            "chapters_took": chapters_took,
            "region": region,
        })

        record_source(facts, SOURCE_AUDIBLE)
        return result

    except NotFoundException:
        raise

    except Exception as e:
        # Try DB first
        db_result = await get_track_from_db(session, asin)
        if db_result:
            record_source(facts, SOURCE_DB)
            return db_result

        # Fall back to cache
        cached = await cache.get(session, chapters_key(asin, region))
        if cached:
            record_source(facts, SOURCE_CACHE)
            return cached

        # Neither a stored copy nor a cached one exists -- that is silence,
        # not a confirmed absence, so what reaches the caller has to say
        # Audible could not be reached rather than that this book has no
        # chapters.
        logger.warning("Audible unavailable and no cached chapter data found", extra={
            "asin": asin,
            "region": region,
            "error": str(e),
            "upstream_status": upstream_status_of(e),
        })
        raise as_audible_failure(
            e, "Audible unavailable and no cached chapter data found"
        ) from e


async def fetch_and_store_chapters(
    asin: str,
    region: str,
    session: AsyncSession,
) -> str:
    """
    Fetches a book's chapters and stores them, stamping chapters_checked_at so the
    book is recorded as checked whatever the outcome. Best-effort: this never
    raises, so a chapter failure can't break whatever persistence flow called it
    (the seeder relies on that — its job is book metadata, chapters are a bonus).

    Outcomes mirror the standalone backfill:
    - "stored":    chapters fetched and written to tracks; marked checked.
    - "none":      resolved but Audible exposes no chapters; marked checked.
    - "not_found": 404 — no chapter metadata anywhere (e.g. the ISBN-keyed
                   records); marked checked so it isn't retried.
    - "error":     transient failure (Audible 500/timeout/network) or a write
                   failure; NOT marked, so a later pass (or the backfill) retries.

    A book asked about before its release date is marked like any other, but
    the mark does not settle it: Audible answers a chapter request for audio
    that does not exist yet with a 404, so both selection sites re-admit a book
    whose stamp predates its release_date once that date has passed. Asking
    early therefore costs one wasted request rather than retiring the title
    before it ever had chapters to find.

    Coordinates with the standalone backfill via chapters_checked_at: neither
    path re-fetches what the other has already marked, on the same terms.
    """
    try:
        data = await fetch_chapter_metadata(audible_get, asin, region)
    except NotFoundException:
        await _mark_chapters_checked(session, asin)
        return "not_found"
    except Exception as e:
        logger.warning(
            "Seeder chapters: fetch error",
            extra={
                "asin": asin,
                "region": region,
                "error_type": type(e).__name__,
                "error": str(e),
            },
        )
        return "error"

    if not has_chapter_info(data):
        await _mark_chapters_checked(session, asin)
        return "none"

    try:
        chapters = normalize_chapters(data)
        await upsert_track(session, asin, chapters)
        await _mark_chapters_checked(session, asin)
        return "stored"
    except Exception as e:
        logger.warning(
            "Seeder chapters: store failed",
            extra={"asin": asin, "error_type": type(e).__name__, "error": str(e)},
        )
        await session.rollback()
        return "error"


async def _mark_chapters_checked(session: AsyncSession, asin: str) -> None:
    """
    Stamps chapters_checked_at on a book, recording that its chapters have
    been asked about.

    Nothing ever clears the column, so for a book that was already out when it
    was asked this is final and it leaves the queue for good. For one asked
    ahead of its release date it is not: both selection sites re-admit a book
    whose stamp predates its release_date once that date has passed. That is
    what keeps an early 404 -- Audible has no chapters for audio that does not
    exist yet -- from retiring a title before release day, and it needs no
    condition here, because the stamp itself is the record of when the
    question was asked. See _gather_chapters in the seeder and _select_work in
    scripts/backfill_chapters.py.
    """
    await session.execute(
        update(Book)
        .where(Book.asin == asin)
        .values(chapters_checked_at=datetime.now(timezone.utc))
    )
    await session.commit()