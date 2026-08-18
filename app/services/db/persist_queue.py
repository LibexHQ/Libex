"""
Background persistence queue.

Decides when a write runs, how many run at once, whose transaction each one
runs in, and what happens when one fails. The statements themselves and every
merge rule they apply belong to writer, which this module imports and which
never imports this one — writer is stateless and this module is not, and the
one-way edge is what keeps the mutable process-wide state (the running-task
registry, the backlog counter, the shed window) out of a module whose whole
contract is "given a session, make these rows say this".
"""

# Standard library
import asyncio
import random
import time
from datetime import timedelta

# Third party
from asyncpg.exceptions import TransactionRollbackError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Database
from app.db.models import Cache
from app.db.session import engine

# Services
from app.services.cache import manager as cache
from app.services.cache.manager import author_key, book_key, chapters_key, series_key
from app.services.db.writer import (
    _cache_set_many,
    _failure_fields,
    _now,
    upsert_author_profile,
    upsert_book,
    upsert_series_profile,
    upsert_track,
    write_books,
)

# Core
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger()
settings = get_settings()

_BackgroundSession = async_sessionmaker(engine, expire_on_commit=False)

# Books written per transaction by the batched background persist. Matched to
# the size of the 50-ASIN chunk the books service already fetches Audible in.
# Small enough that the row locks a chunk holds are released promptly for the
# seeder and for concurrent requests touching the same authors, genres and
# narrators, and that replaying a chunk after a lost transaction is cheap;
# large enough that persisting a prolific author's 1000-book catalog costs 20
# commits instead of 2000.
#
# Two things bound it from above, and neither shows in the tradeoff above.
# Subtransactions: upsert_author opens a SAVEPOINT for each asin-less author it
# inserts and each null-asin row it upgrades, and both branches sit behind a
# SELECT that runs first, so a repeat author inside a chunk never reaches
# either branch — write_books resolves each distinct author once per chunk and
# reuses the id, and even without that memo the SELECT would have found the
# transaction's own uncommitted insert. A chunk therefore holds one savepoint
# per distinct author in it that is new or still null-asin, not one per book:
# fifty books of a prolific author's catalog open about one, while a batch of
# unrelated new books opens one for every author it carries.
# Postgres's 64-entry per-backend subxid cache takes 65 distinct unseen authors
# in a single chunk to cross — two of them a book fills it at 32 books and
# passes it at 33 — after which snapshots taken while the transaction is open
# carry the suboverflowed flag and other backends resolve subtransaction
# visibility through the pg_subtrans SLRU rather than the cache. Bind
# parameters: _cache_set_many puts four per entry into one INSERT against
# asyncpg's 32,767, so a chunk of 8192 raises before the commit — nothing is
# lost, since _persist_book_chunk_background replays the chunk book by book,
# but it discards the batch it just wrote and lands on the per-book path the
# batching exists to avoid. It is the only multi-row VALUES on this path; every other
# statement binds one row per execution and cannot reach the cap at any chunk
# size. The two sit differently at 50: the INSERT binds 200 parameters and is
# two orders of magnitude clear, while the subxid cache holds for a catalog
# chunk and a wide enough mix of unseen authors passes it. Passing it costs
# SLRU lookups — a throughput cost, not a limit, and only on a cold catalog,
# since re-writing the same books finds the authors stored and opens no
# savepoints. Only the bind ceiling is a hard stop on raising the constant.
_PERSIST_CHUNK_SIZE = 50

# How many times a chunk's transaction is attempted before it falls back to
# writing the chunk a book at a time.
#
# Three, because a bounded attempt count is the only thing bounding how long a
# chunk's work can stay in flight. statement_timeout bounds one statement, not
# a transaction, and idle_in_transaction_session_timeout only fires on a
# transaction that is idle — a retrying one never is, because each attempt
# opens a fresh transaction and closes it before the backoff. Nothing else
# would stop a pathologically contended chunk from holding the oldest xmin on
# a 1.1M-row table and stalling autovacuum's ability to clean up behind it.
_PERSIST_MAX_ATTEMPTS = 3

# First backoff step, doubled per attempt and jittered across half to one and
# a half of the nominal figure. The jitter is the point: six worker processes
# retrying a deadlock in lockstep re-collide on the same rows, and the whole
# purpose of backing off is to arrive at a different time from whoever won.
# Worst case across the two sleeps a chunk can take is about 1.1 seconds.
_PERSIST_RETRY_BASE_SECONDS = 0.25

# Books allowed to be queued or in flight at once, per worker process.
#
# Measured in books rather than tasks because a task is anything from one book
# to a whole prolific catalog, so a task bound tells you nothing about what is
# actually being held. Every queued book is a normalized payload the response
# has already been built from and sent, so the backlog is retained memory and
# nothing else — the caller is never made to wait on it.
#
# The figure is a ceiling, not a throttle. A chunk of fifty now costs a couple
# of dozen statements and commits in tens of milliseconds — about 2,800 books
# a second per worker — which no upstream can feed, so the queue sits near
# empty and normal traffic never approaches this. What builds a backlog is
# Postgres being degraded: attempts start failing, each chunk spends its three
# tries and two backoffs and then replays fifty books one at a time, and the
# arrival rate stops being the thing that sets the pace. Every response still
# succeeds while that happens, so nothing upstream slows down either.
#
# 5,000 comes from the arithmetic rather than a round number. A normalized book
# measured 8,956 bytes held in memory (3,297 serialized), so this is about 45 MB
# per worker and 269 MB across the six, as an absolute worst case reached only
# while the database is unavailable. It is also two full prolific catalogs, so
# a second walk arriving while the first is still draining is admitted rather
# than shed whole. Raising it trades that headroom against the one failure here
# that takes a worker down completely: shedding costs some books a write they
# get back on the next request, and running out of memory costs everything the
# process was doing.
_PERSIST_BACKLOG_MAX_BOOKS = 5000

# Shedding and chunk retries are both reported on this window, never per
# event. The log pipeline drops silently once its queue passes 2000, so a
# line per shed book — or per retrying chunk, across six worker processes
# under sustained Postgres degradation — would flood out the record of the
# exact incident it is reporting, including its own first line.
_SHED_LOG_INTERVAL_SECONDS = 60

# asyncio.Semaphore binds its internal waiter state to whichever event loop is
# running the first time it's touched, so one built at import time and reused
# across loops can be left holding waiters queued against a loop that has since
# closed. Under uvicorn that is one long-lived loop and the risk is theoretical;
# the retry path is what makes it real, because it is the first thing here that
# can put a waiter on the queue at all — until now a permit was always
# available or the wait was momentary. A semaphore whose waiters belong to a
# dead loop does not warn, it hangs. Keying the instance to the running loop
# and rebuilding when that loop changes costs one identity check per acquire.
_bg_write_semaphore: asyncio.Semaphore | None = None
_bg_write_semaphore_loop: asyncio.AbstractEventLoop | None = None

# Limits concurrent background writes so the seeder doesn't starve the
# API's DB connection pool. At most 2 background persist tasks write at once.
_BG_WRITE_CONCURRENCY_LIMIT = 2

# Strong references to every background task in flight. asyncio holds only a
# weak reference to a running task, so a task nothing else refers to can be
# garbage collected mid-await and simply stop, with no error anywhere — every
# entry point here fires and forgets, so without this set that is exactly what
# they are. Retries make it worse rather than introducing it: a task now lives
# across up to three attempts and two backoffs instead of one round trip.
_inflight: set[asyncio.Task] = set()

# Books currently queued or in flight, and the shed accounting the window
# reports. Plain module state, mutated only from the event loop thread.
_queued_books = 0
_shed_books = 0
_shed_events = 0
_shed_last_logged = 0.0

# Chunk-retry accounting, windowed the same way and for the same reason as
# shedding — see _record_retry.
_retry_attempts = 0
_retry_chunks_replayed = 0
_retry_last_logged = 0.0


# ============================================================
# QUEUE STATE ACCESSORS
# ============================================================
#
# Queue depth and queue capacity are legitimate public questions about a
# queue. These exist so a caller outside this module (a seeder deriving its
# own backpressure guard from the real ceiling, rather than restating it as a
# number that silently drifts out of sync the next time this module's
# constant changes) has something to read instead of reaching past the
# underscore into module-private state.

def backlog_capacity() -> int:
    """The books-in-flight ceiling this module enforces before it starts
    shedding — see _PERSIST_BACKLOG_MAX_BOOKS for how it was sized."""
    return _PERSIST_BACKLOG_MAX_BOOKS


def queued_books() -> int:
    """Books currently queued or in flight across every entry point below."""
    return _queued_books


def _get_bg_write_semaphore() -> asyncio.Semaphore:
    global _bg_write_semaphore, _bg_write_semaphore_loop
    loop = asyncio.get_running_loop()
    if _bg_write_semaphore is None or _bg_write_semaphore_loop is not loop:
        _bg_write_semaphore = asyncio.Semaphore(_BG_WRITE_CONCURRENCY_LIMIT)
        _bg_write_semaphore_loop = loop
    return _bg_write_semaphore


def _window_elapsed(last_logged: float, now: float) -> bool:
    """True at most once per _SHED_LOG_INTERVAL_SECONDS since last_logged —
    the gate shared by every incident-volume log line on this path."""
    return now - last_logged >= _SHED_LOG_INTERVAL_SECONDS


def _record_shed(books: int) -> None:
    """
    Counts a shed batch and reports the running total at most once per window.

    The first shed of a quiet process reports immediately and opens the window;
    everything shed inside that window is silent and lands in the total the
    line that closes it carries. Reporting the start at once is deliberate —
    waiting out a window before saying anything hides the moment shedding
    began, which is the part that dates the incident.

    The count is what matters operationally — which books were dropped is not
    recoverable information anyway, since they persist on the next request that
    asks for them — so nothing about a book is logged, only how many.
    """
    global _shed_books, _shed_events, _shed_last_logged

    _shed_books += books
    _shed_events += 1

    now = time.monotonic()
    if not _window_elapsed(_shed_last_logged, now):
        return

    logger.warning("Background persist shedding", extra={
        "shed_books": _shed_books,
        "shed_batches": _shed_events,
        "queued_books": _queued_books,
        "backlog_limit": _PERSIST_BACKLOG_MAX_BOOKS,
    })
    _shed_books = 0
    _shed_events = 0
    _shed_last_logged = now


def _record_retry(attempts: int, replayed: bool, fields: dict) -> None:
    """
    Counts one chunk's retry attempts and, when it exhausted them and fell to
    per-book replay, counts that too — reported at most once per window, the
    same cap _record_shed applies and for the same reason: sustained Postgres
    degradation retries every chunk across six worker processes, and an
    uncapped line per attempt during that exact incident is what floods the
    pipeline that would otherwise carry the report of it.

    fields carries the triggering failure's _failure_fields — schema metadata
    only, never row content — so the report that closes a window still says
    what kind of failure drove it, even though the per-attempt detail before
    it is silently folded into the totals.
    """
    global _retry_attempts, _retry_chunks_replayed, _retry_last_logged

    _retry_attempts += attempts
    if replayed:
        _retry_chunks_replayed += 1

    now = time.monotonic()
    if not _window_elapsed(_retry_last_logged, now):
        return

    logger.warning("Background persist chunk retries", extra={
        "retry_attempts": _retry_attempts,
        "chunks_replayed": _retry_chunks_replayed,
        **fields,
    })
    _retry_attempts = 0
    _retry_chunks_replayed = 0
    _retry_last_logged = now


def _spawn(make_coro, books: int) -> None:
    """
    Starts one background persist, or sheds it when the backlog is full.

    Takes a coroutine function rather than a coroutine so a shed batch leaves
    nothing un-awaited behind it. The task is held in _inflight for as long as
    it runs and discarded by its own done callback.
    """
    global _queued_books

    if _queued_books + books > _PERSIST_BACKLOG_MAX_BOOKS:
        _record_shed(books)
        return

    _queued_books += books
    task = asyncio.create_task(_tracked(make_coro(), books))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


async def _tracked(coro, books: int) -> None:
    """Releases the backlog the task reserved, however the task ends."""
    global _queued_books
    try:
        await coro
    finally:
        _queued_books -= books


def _is_retryable(exc: BaseException) -> bool:
    """
    Whether a fresh transaction stands a real chance where this one failed.

    Three conditions, each for a reason a later attempt genuinely fixes:

    - Pool exhaustion. sqlalchemy.exc.TimeoutError subclasses SQLAlchemyError
      directly and is NOT a DBAPIError, so it has to be named on its own — a
      classifier written against OperationalError and InterfaceError misses
      the single condition this layer most exists to survive.
    - Serialization failures and deadlocks, discriminated by asyncpg's
      TransactionRollbackError, the common base of SerializationError (40001)
      and DeadlockDetectedError (40P01). Matching the base rather than
      enumerating SQLSTATE strings means a sibling added upstream is covered
      without an edit here.
    - An invalidated connection, which is SQLAlchemy's own signal that the
      connection died under the statement rather than the statement being
      wrong. A new session takes a new connection.

    Everything else — a constraint violation, a value the schema rejects, a
    bug — fails identically on every attempt, so retrying it only delays the
    per-book replay that can actually isolate the one bad book.
    """
    if isinstance(exc, PoolTimeoutError):
        return True
    if isinstance(exc, DBAPIError):
        if exc.connection_invalidated:
            return True
        return isinstance(getattr(exc, "orig", None), TransactionRollbackError)
    return False


def _backoff_seconds(attempt: int) -> float:
    """Exponential in the attempt, jittered across half to one and a half."""
    nominal = _PERSIST_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
    return nominal * (0.5 + random.random())


# ============================================================
# BOOK CHUNKS
# ============================================================

async def _write_book_chunk(session: AsyncSession, chunk: list[dict], region: str) -> None:
    """One chunk's books and cache entries, committed together. Raises."""
    await write_books(session, chunk)
    await _cache_set_many(session, [(book_key(b["asin"], region), b) for b in chunk])
    await session.commit()
    logger.info(f"DB write: {len(chunk)} books")


async def _replay_book_chunk(session: AsyncSession, chunk: list[dict], region: str) -> None:
    """The chunk written a book at a time, each in a transaction of its own."""
    for book in chunk:
        await upsert_book(session, book)
        await cache.set(session, book_key(book["asin"], region), book)


async def _attempt_book_chunk(chunk: list[dict], region: str) -> Exception | None:
    """
    One attempt at a chunk, in a permit and a session of its own, both released
    before it returns whether it succeeded or not.

    Returns the failure rather than raising it so the caller can decide about
    a retry with nothing held: the backoff must not sleep inside an open
    transaction — past idle_in_transaction_session_timeout the server
    terminates the backend and the next statement fails on a closed connection
    — and must not sleep holding a permit or a pooled connection either, since
    the pool is what starves first with six workers against a shared limit.
    Sleeping while still holding the chunk's row locks would also guarantee
    the retry collides with whoever it collided with the first time.
    """
    async with _get_bg_write_semaphore():
        async with _BackgroundSession() as session:
            try:
                await _write_book_chunk(session, chunk, region)
                return None
            except Exception as exc:
                await session.rollback()
                return exc


async def _persist_book_chunk_background(chunk: list[dict], region: str) -> None:
    """
    Writes one chunk in the background, retrying a contended transaction in a
    fresh session before giving up and replaying the chunk book by book.

    The retry loop sits outside both context managers on purpose, so the
    backoff holds no permit, no session and no connection.
    """
    for attempt in range(1, _PERSIST_MAX_ATTEMPTS + 1):
        failure = await _attempt_book_chunk(chunk, region)
        if failure is None:
            return

        exhausted = attempt == _PERSIST_MAX_ATTEMPTS or not _is_retryable(failure)
        _record_retry(attempt, exhausted, _failure_fields(failure))
        if exhausted:
            break

        await asyncio.sleep(_backoff_seconds(attempt))

    async with _get_bg_write_semaphore():
        async with _BackgroundSession() as session:
            await _replay_book_chunk(session, chunk, region)


# ============================================================
# ENTRY POINTS
# ============================================================

def persist_books_background(books: list[dict], region: str) -> None:
    """
    Fires a single background task to write multiple books to DB and cache.

    Writes in transactions of _PERSIST_CHUNK_SIZE books rather than one
    transaction per book. A prolific author's catalog runs past a thousand
    books, and committing per book — twice per book, once for the row and
    again for its cache entry — costs thousands of separate transactions for
    it; the commits, not the statements, are the cost. For what partial
    progress survives a failure, see _persist_book_chunk_background.

    Books without an ASIN are dropped before the slicing, so the backlog is
    reserved for what will actually be written and no chunk is short.
    """
    persistable = [b for b in books if b.get("asin")]

    async def _persist():
        try:
            for start in range(0, len(persistable), _PERSIST_CHUNK_SIZE):
                await _persist_book_chunk_background(
                    persistable[start:start + _PERSIST_CHUNK_SIZE],
                    region,
                )
        except Exception as exc:
            # Every failure a chunk can foresee is already handled inside it,
            # down to the per-book replay. This catches what it cannot: nothing
            # else is waiting on this task, so an escaping exception would
            # otherwise surface only as asyncio's "never retrieved" notice at
            # collection time, detached from the work that raised it.
            logger.warning(
                "Background persist failed for a book batch",
                extra={"books": len(persistable), "region": region, **_failure_fields(exc)},
            )

    _spawn(_persist, len(persistable))


def persist_author_background(data: dict, region: str) -> None:
    """Fires a background task to write an author profile to DB and cache."""
    async def _persist():
        async with _get_bg_write_semaphore():
            try:
                async with _BackgroundSession() as session:
                    await upsert_author_profile(session, data)
                    if data.get("asin"):
                        await cache.set(session, author_key(data["asin"], region), data)
            except Exception as e:
                logger.warning(
                    "Background persist failed for author",
                    extra={"asin": data.get("asin"), **_failure_fields(e)},
                )

    _spawn(_persist, 1)


def persist_series_background(data: dict, region: str) -> None:
    """Fires a background task to write a series profile to DB and cache."""
    async def _persist():
        async with _get_bg_write_semaphore():
            try:
                async with _BackgroundSession() as session:
                    await upsert_series_profile(session, data)
                    if data.get("asin"):
                        await cache.set(session, series_key(data["asin"], region), data)
            except Exception as e:
                logger.warning(
                    "Background persist failed for series",
                    extra={"asin": data.get("asin"), **_failure_fields(e)},
                )

    _spawn(_persist, 1)


def persist_track_background(asin: str, chapters_data: dict, region: str) -> None:
    """Fires a background task to write chapter data to DB and cache."""
    async def _persist():
        async with _get_bg_write_semaphore():
            try:
                async with _BackgroundSession() as session:
                    await upsert_track(session, asin, chapters_data)
                    await cache.set(session, chapters_key(asin, region), chapters_data)
            except Exception as e:
                logger.warning(
                    "Background persist failed for track",
                    extra={"asin": asin, **_failure_fields(e)},
                )

    _spawn(_persist, 1)


def persist_cache_background(key: str, value) -> None:
    """Fires a background task to write a single cache entry."""
    async def _persist():
        async with _get_bg_write_semaphore():
            try:
                async with _BackgroundSession() as session:
                    await cache.set(session, key, value)
            except Exception as e:
                logger.warning(
                    "Background cache persist failed",
                    extra={"cacheKey": key, **_failure_fields(e)},
                )

    _spawn(_persist, 1)


def persist_author_books_cache_background(
    key: str, asins: list[str], ttl_seconds: int | None = None
) -> None:
    """
    Fires a background task to write an author's book-ASIN list to cache.

    An author's ASIN list only ever grows — it never legitimately shrinks —
    so the write is the union of what's already stored and what just came
    in, never a straight replacement. The catalog sort windows this list is
    built from shift as new titles are released, so a later, equal-or-longer
    run legitimately surfaces new ASINs while its own window pushes older
    ones out; comparing the two lists for one to be a superset of the other
    would refuse that normal case and discard the very ASINs it's meant to
    protect. The union orders incoming ASINs first, then appends any
    stored-only ones, since list order is a consumer-visible contract here.

    The stored row is locked with SELECT ... FOR UPDATE before the union is
    built, and the write happens later in the same transaction that took the
    lock, so a second concurrent call for the same key blocks on the lock
    until the first call's write has committed, then unions against that
    just-written value instead of the stale one. This only protects a key
    that already has a row: two calls racing to write the very first value
    for a brand-new key still both proceed unconditionally, the same as any
    other first-write upsert in this module, since there is nothing stored
    yet to union with.

    A stored row past its expiry is still locked (so the lock keeps working),
    but is treated as not-stored for the union, matching cache.get's own
    expired-is-a-miss behavior — this guard does not protect an expired entry.

    The expiry gets the same can-only-grow discipline as the ASIN list: the
    write keeps whichever is later, the stored row's expires_at or the one
    this call is asking for, rather than letting a plain overwrite apply
    the incoming TTL regardless of what's already been earned. That is
    deliberate, not an oversight of the union guarantee stopping at content:
    the two callers who actually gate a walk behind this key's TTL —
    get_author_books' use_cache=True path, and the total-failure fallback
    inside _walk_author_books itself — only ever reach this row after
    cache.get already reported a miss, i.e. after the stored expires_at has
    already passed. Keeping the later expiry costs neither of them anything:
    there is no case where it makes an already-expired, about-to-be-refreshed
    entry look fresher than it is. What it does protect is the other,
    unthrottled caller: get_author_books' default, Audible-first path calls
    this function on every request regardless of the stored TTL, so a
    previously-earned 24h window sat behind a row that is still very much
    live can otherwise be clobbered down to the short, degraded-run TTL by
    the very next request that happens to hit one bad page out of hundreds —
    and, since nothing else ever re-lengthens a downgraded entry, that
    ratchets permanently. Content and trustworthiness window move together:
    a degraded run's result is provably never worse than what's stored (it's
    a union, never a shrink), so there is no basis for treating it as less
    trustworthy either. The cost: an explicitly-cached reader that would
    otherwise be forced to retry within the short TTL instead waits out the
    remainder of the earned window — bounded by the union guarantee to
    "missing very recent additions," never "holding data since superseded."

    ttl_seconds is passed through as the request, not the outcome: since
    this write is always a union that can only grow the stored list, never
    shrink it, a caller who knows this run's own result is incomplete can
    still write it with a shorter TTL than the default so it refreshes
    sooner, rather than withholding the write entirely — the later-wins rule
    above is what stops that shorter request from undoing a longer window
    still in force. None keeps cache.set's own default (settings.cache_ttl).
    """
    async def _persist():
        async with _get_bg_write_semaphore():
            try:
                async with _BackgroundSession() as session:
                    locked = await session.execute(
                        select(Cache).where(Cache.key == key).with_for_update()
                    )
                    row = locked.scalar_one_or_none()
                    now = _now()
                    stored = row.value if row and row.expires_at > now else None
                    to_write = asins
                    if stored:
                        incoming_set = set(asins)
                        stored_only = [a for a in stored if a not in incoming_set]
                        if stored_only:
                            to_write = list(asins) + stored_only
                    requested_ttl = ttl_seconds if ttl_seconds is not None else settings.cache_ttl
                    requested_expires_at = now + timedelta(seconds=requested_ttl)
                    if row is not None and row.expires_at > requested_expires_at:
                        final_ttl = (row.expires_at - now).total_seconds()
                    else:
                        final_ttl = requested_ttl
                    await cache.set(session, key, to_write, ttl_seconds=final_ttl)
            except Exception as e:
                logger.warning(
                    "Background cache persist failed",
                    extra={"cacheKey": key, **_failure_fields(e)},
                )

    _spawn(_persist, 1)
