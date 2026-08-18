"""
Background author-books completion tests.

The module exists so that a walk which ran out of time on a live request is
finished off the request and the COMPLETE result is what gets cached. Its
whole value is in what it refuses to do twice, what it gives up on, and what
it declines to store -- so that is what is pinned here, rather than the
happy path alone.
"""

# Standard library
import asyncio
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
import app.services.audible.authors.completion as completion
from app.services.audible.authors import AuthorBooksResult


@pytest.fixture(autouse=True)
def _clean_registries():
    """Module state is process-wide and would otherwise leak between tests --
    an attempt counter left behind by one test silently suppresses the
    completion another test is asserting on."""
    completion._completion_inflight.clear()
    completion._completion_attempts.clear()
    yield
    completion._completion_inflight.clear()
    completion._completion_attempts.clear()


def _walk_returning(result, calls):
    async def _walk(asin, region, session, *, time_budget, allow_background_completion):
        calls.append({
            "asin": asin,
            "time_budget": time_budget,
            "allow_background_completion": allow_background_completion,
        })
        return result
    return _walk


async def _drain():
    """Lets the fire-and-forget task actually run to completion."""
    for _ in range(20):
        if not completion._completion_inflight:
            return
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_completion_runs_the_walk_with_a_budget_no_caller_is_waiting_on():
    """The reason a completion can finish a walk a request could not is that
    it is not bounded by what a caller will sit through. If it inherited the
    live budget it would truncate in exactly the same place and the whole
    mechanism would be a no-op that looks like it works."""
    calls = []
    walk = _walk_returning(AuthorBooksResult(["B0A", "B0B"], True), calls)

    with patch("app.services.audible.authors._walk_author_books", new=walk), \
         patch.object(completion, "_CompletionSession"):
        completion.request_author_books_completion("B000AUTHOR", "us", ["B0A"])
        await _drain()

    assert len(calls) == 1
    assert calls[0]["time_budget"] == completion._COMPLETION_TIME_BUDGET_SECONDS
    assert calls[0]["time_budget"] > 60


@pytest.mark.asyncio
async def test_a_completion_cannot_ask_for_another_completion():
    """Without this the completion's own truncated result would request a
    completion, and an author who can never finish would recurse."""
    calls = []
    walk = _walk_returning(AuthorBooksResult(["B0A"], False), calls)

    with patch("app.services.audible.authors._walk_author_books", new=walk), \
         patch.object(completion, "_CompletionSession"):
        completion.request_author_books_completion("B000AUTHOR", "us", ["B0A"])
        await _drain()

    assert calls[0]["allow_background_completion"] is False


@pytest.mark.asyncio
async def test_a_second_request_while_one_is_running_is_dropped():
    """Six truncated requests for the same prolific author must cost one
    completion, not six. Single-flight upstream only collapses genuinely
    concurrent requests; this has to hold across sequential ones for as long
    as the completion runs, which is the case that actually happens."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def _walk(asin, region, session, *, time_budget, allow_background_completion):
        calls.append(asin)
        started.set()
        await release.wait()
        return AuthorBooksResult(["B0A"], True)

    with patch("app.services.audible.authors._walk_author_books", new=_walk), \
         patch.object(completion, "_CompletionSession"):
        completion.request_author_books_completion("B000AUTHOR", "us", ["B0A"])
        await started.wait()
        completion.request_author_books_completion("B000AUTHOR", "us", ["B0A"])
        completion.request_author_books_completion("B000AUTHOR", "us", ["B0A"])
        assert completion.inflight_count() == 1
        release.set()
        await _drain()

    assert calls == ["B000AUTHOR"]


@pytest.mark.asyncio
async def test_a_different_author_is_not_blocked_by_one_in_flight():
    """The dedupe is per author, not a global lock -- otherwise one prolific
    author's completion would suppress everyone else's."""
    release = asyncio.Event()
    calls = []

    async def _walk(asin, region, session, *, time_budget, allow_background_completion):
        calls.append(asin)
        await release.wait()
        return AuthorBooksResult([], True)

    with patch("app.services.audible.authors._walk_author_books", new=_walk), \
         patch.object(completion, "_CompletionSession"):
        completion.request_author_books_completion("B000AUTHOR1", "us", [])
        completion.request_author_books_completion("B000AUTHOR2", "us", [])
        await asyncio.sleep(0)
        assert completion.inflight_count() == 2
        release.set()
        await _drain()

    assert sorted(calls) == ["B000AUTHOR1", "B000AUTHOR2"]


@pytest.mark.asyncio
async def test_the_same_author_in_another_region_is_a_separate_completion():
    """Keyed by (asin, region) because an author's catalogue is per
    marketplace -- a completed us walk says nothing about de."""
    calls = []
    walk = _walk_returning(AuthorBooksResult([], True), calls)

    with patch("app.services.audible.authors._walk_author_books", new=walk), \
         patch.object(completion, "_CompletionSession"):
        completion.request_author_books_completion("B000AUTHOR", "us", [])
        completion.request_author_books_completion("B000AUTHOR", "de", [])
        await _drain()

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_an_author_who_never_finishes_stops_being_retried():
    """The cap is what stops an author whose walk can never complete being
    re-walked on every truncated request forever -- which would be worse
    than the behaviour this replaced."""
    calls = []
    walk = _walk_returning(AuthorBooksResult(["B0A"], False), calls)

    with patch("app.services.audible.authors._walk_author_books", new=walk), \
         patch.object(completion, "_CompletionSession"):
        for _ in range(6):
            completion.request_author_books_completion("B000AUTHOR", "us", ["B0A"])
            await _drain()

    assert len(calls) == completion._COMPLETION_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_giving_up_caches_nothing():
    """The invariant the caller-facing header depends on: only a finished
    walk is ever written to the author-books key. Storing the partial here
    would be the cheap thing to do and would quietly make every cache hit
    unreportable as complete."""
    walk = _walk_returning(AuthorBooksResult(["B0A"], False), [])

    with patch("app.services.audible.authors._walk_author_books", new=walk), \
         patch.object(completion, "_CompletionSession"), \
         patch("app.services.db.persist_queue.persist_author_books_cache_background") as mock_persist:
        for _ in range(completion._COMPLETION_MAX_ATTEMPTS + 1):
            completion.request_author_books_completion("B000AUTHOR", "us", ["B0A"])
            await _drain()

    mock_persist.assert_not_called()


@pytest.mark.asyncio
async def test_a_successful_completion_clears_the_attempt_ledger():
    """So an author who truncates again months later gets a fresh set of
    attempts rather than being permanently written off by history."""
    walk = _walk_returning(AuthorBooksResult(["B0A", "B0B"], True), [])

    with patch("app.services.audible.authors._walk_author_books", new=walk), \
         patch.object(completion, "_CompletionSession"):
        completion.request_author_books_completion("B000AUTHOR", "us", ["B0A"])
        await _drain()

    assert ("B000AUTHOR", "us") not in completion._completion_attempts


@pytest.mark.asyncio
async def test_a_raising_walk_does_not_wedge_the_registry():
    """A completion that blows up must release its key, or that author can
    never be completed again for the life of the process."""
    async def _walk(asin, region, session, *, time_budget, allow_background_completion):
        raise RuntimeError("Audible down")

    with patch("app.services.audible.authors._walk_author_books", new=_walk), \
         patch.object(completion, "_CompletionSession"):
        completion.request_author_books_completion("B000AUTHOR", "us", ["B0A"])
        await _drain()

    assert completion.inflight_count() == 0
