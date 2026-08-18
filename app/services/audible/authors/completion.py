"""
Background completion of author-books walks that ran out of time.

A live author-books request is bounded by what a caller will wait for, and
the fronting proxy gives up at 30 seconds regardless. A prolific author's
walk can exceed that, and when it does the request has a partial ASIN list
and a choice: hand it over as though it were the answer, or hand it over
and say so. It says so -- and then finishes the job here, off the request,
with a budget nobody is waiting on, so that the NEXT caller gets a complete
answer out of the cache rather than the same partial one.

This module owns the mutable process-wide state that arrangement needs: the
in-flight registry that stops six simultaneous truncated requests becoming
six simultaneous completion walks, the attempt counter that stops an author
who can never finish being retried forever, and the concurrency bound that
stops completions competing with live traffic for the Audible lane. The
walk itself stays in __init__.py and knows none of this, the same one-way
split writer and persist_queue already use.
"""

# Standard library
import asyncio

# Third party
from sqlalchemy.ext.asyncio import async_sessionmaker

# Database
from app.db.session import engine

# Core
from app.core.logging import get_logger

logger = get_logger()

# A session of its own, never the request's. The live walk deliberately
# borrows the caller's session and dies with it -- see get_author_books'
# note on why its leader await is unshielded. A completion outlives the
# request that triggered it by design, so borrowing that session would
# leave it querying one whose context manager has already exited, which
# does not fail loudly: it silently checks a fresh connection out of the
# pool with nothing left to return it.
_CompletionSession = async_sessionmaker(engine, expire_on_commit=False)

# How long a completion walk may take. Far above the live budget because
# the point of running here is that no caller is waiting -- but bounded,
# because an unbounded walk against a degraded upstream would hold a
# connection and a slot indefinitely.
_COMPLETION_TIME_BUDGET_SECONDS = 300.0

# Completion walks allowed to run at once, per worker process.
#
# One. A completion is the single most expensive operation Libex performs --
# the same hundreds of upstream requests a live walk makes -- and it is by
# definition work no caller is waiting for. Letting two run concurrently in
# a process would have background work competing with live requests for the
# Audible lane, which is the exact contention that produced the 504s this
# whole effort exists to remove. Six workers means at most six across the
# deployment, which is already generous for work that is never urgent.
_COMPLETION_CONCURRENCY_LIMIT = 1

# Attempts before a given author is left alone.
#
# Two, because the failure this guards against is not transient. If a walk
# cannot finish inside five minutes with nobody waiting, the cause is the
# author's catalogue size or an upstream that is degraded, and neither is
# fixed by going round again. Without a cap, an author who can never finish
# would be re-walked on every truncated request forever, which is worse
# than the behaviour this replaced.
_COMPLETION_MAX_ATTEMPTS = 2

# Bound on the attempt ledger. It is keyed by (asin, region) and only ever
# grows, so a long-running process walking a wide catalogue would otherwise
# accumulate an entry per author that ever truncated. Cleared wholesale
# rather than evicted one by one: the counter's only job is to stop a tight
# retry loop within a single episode, so losing the history is a reset to
# "try again", not a loss of anything that needed keeping.
_COMPLETION_ATTEMPTS_MAX_TRACKED = 10000

_completion_inflight: dict[tuple[str, str], asyncio.Task] = {}
_completion_attempts: dict[tuple[str, str], int] = {}

# Keyed to the running loop for the same reason persist_queue's is: a
# Semaphore binds its waiter state to whichever loop first touches it, and
# one built at import time and reused across loops can be left holding
# waiters queued against a loop that has closed. That does not warn, it
# hangs.
_completion_semaphore: asyncio.Semaphore | None = None
_completion_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _get_completion_semaphore() -> asyncio.Semaphore:
    global _completion_semaphore, _completion_semaphore_loop
    loop = asyncio.get_running_loop()
    if _completion_semaphore is None or _completion_semaphore_loop is not loop:
        _completion_semaphore = asyncio.Semaphore(_COMPLETION_CONCURRENCY_LIMIT)
        _completion_semaphore_loop = loop
    return _completion_semaphore


def inflight_count() -> int:
    """Completion walks queued or running in this process. Exists so a
    caller outside this module can read the depth without reaching past the
    underscore into module-private state."""
    return len(_completion_inflight)


def _give_up(asin: str, region: str, partial_asins: list[str], attempts: int) -> None:
    """
    Records that completion has stopped trying for this author.

    Deliberately writes nothing to the cache, and that is the whole point.
    Storing the partial here would be the obvious thing to do -- it is what
    the old degraded TTL did, and it would spare a pathological author from
    re-walking on every request -- but it would also break the one property
    the caller-facing signal depends on: that anything read back from this
    key is a finished walk. Break it here and a cache hit can no longer be
    reported as complete, which means no response can be reported as
    complete, which means nothing is safe to hand to an edge cache. A rule
    with an exception in it is not a rule the route can act on.

    So the cost is taken where it is visible instead: an author whose walk
    can never finish is never cached, and every request for them re-runs
    the walk and returns a partial that is explicitly marked partial. That
    is expensive and it is honest, where the alternative is cheap and
    quietly wrong. Making it both cheap AND honest means storing
    completeness alongside the value rather than inferring it from the
    key, which changes the cached value's shape and the union in
    persist_author_books_cache_background that reads it -- a larger change
    than this one and a deliberate decision of its own.

    The WARNING is the operational signal that an author has reached this
    state, since nothing else will now report it.
    """
    logger.warning("Author books completion gave up; this author will not be cached", extra={
        "author_asin": asin,
        "region": region,
        "attempts": attempts,
        "author_book_num": len(partial_asins),
    })


async def _complete(asin: str, region: str, partial_asins: list[str]) -> None:
    """
    Re-runs one author's walk with a budget no caller is waiting on.

    The walk writes its own cache entry when it finishes complete, so there
    is nothing to store here on success -- this only has to decide what
    happens when it does not.
    """
    # Deferred rather than a module-scope import, because __init__.py
    # imports this module at its own import time. Restructuring to avoid it
    # would mean moving the walk out of __init__.py, which is a larger
    # change than this one and buys nothing here: the deferred import runs
    # once, inside a task, long after both modules are loaded.
    from app.services.audible.authors import _walk_author_books

    key = (asin, region)
    attempts = _completion_attempts.get(key, 0) + 1
    _completion_attempts[key] = attempts

    try:
        async with _get_completion_semaphore():
            async with _CompletionSession() as session:
                result = await _walk_author_books(
                    asin,
                    region,
                    session,
                    time_budget=_COMPLETION_TIME_BUDGET_SECONDS,
                    # Without this the completion's own truncated result
                    # would request another completion, and an author who
                    # cannot finish would recurse until the attempt cap
                    # happened to catch it -- if it caught it at all, since
                    # each new request would be a fresh key lookup.
                    allow_background_completion=False,
                )
    except Exception as e:
        logger.warning("Author books completion failed", extra={
            "author_asin": asin,
            "region": region,
            "attempts": attempts,
            "error_type": type(e).__name__,
        })
        if attempts >= _COMPLETION_MAX_ATTEMPTS:
            _give_up(asin, region, partial_asins, attempts)
        return

    if result.is_complete:
        logger.info("Author books completion finished the walk", extra={
            "author_asin": asin,
            "region": region,
            "attempts": attempts,
            "author_book_num": len(result.asins),
            "recovered_asins": len(result.asins) - len(partial_asins),
        })
        _completion_attempts.pop(key, None)
        return

    if attempts >= _COMPLETION_MAX_ATTEMPTS:
        # The completion's own union is at least as good as the request's,
        # since both are unions over the same four sources and this one had
        # longer to run.
        _give_up(asin, region, result.asins, attempts)


def request_author_books_completion(asin: str, region: str, partial_asins: list[str]) -> None:
    """
    Asks for one author's truncated walk to be finished in the background.

    Fire and forget, and safe to call on every truncated request: a second
    call for an author already being completed is dropped rather than
    queued, so a burst of truncated requests for the same prolific author
    costs one completion, not one per request. Single-flight upstream only
    collapses genuinely concurrent requests; this has to hold across
    sequential ones too, for as long as the completion runs.

    Like every registry here this is per worker process, so six workers can
    each hold their own completion for the same author. That is the same
    property _author_books_inflight has and is bounded the same way -- by
    the per-process concurrency limit -- rather than by cross-process
    coordination, which Libex has nowhere to put.
    """
    key = (asin, region)

    if key in _completion_inflight:
        return

    if _completion_attempts.get(key, 0) >= _COMPLETION_MAX_ATTEMPTS:
        return

    if len(_completion_attempts) > _COMPLETION_ATTEMPTS_MAX_TRACKED:
        _completion_attempts.clear()

    task = asyncio.ensure_future(_complete(asin, region, list(partial_asins)))
    # Registered before the done callback is attached, and held for the
    # task's whole life: asyncio keeps only a weak reference to a running
    # task, so a fire-and-forget task nothing else refers to can be
    # collected mid-await and simply stop, with no error anywhere.
    _completion_inflight[key] = task
    task.add_done_callback(lambda _t, k=key: _completion_inflight.pop(k, None))
