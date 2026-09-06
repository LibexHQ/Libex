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
from app.services.audible.authors import AuthorBooksResult, _walk_author_books
from app.services.audible.authors.screens import _ScreenBooksResult, SCREENS_REASON_COMPLETED


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
async def test_a_full_queue_sheds_rather_than_growing_without_bound():
    """The concurrency limit bounds how many completions RUN; this bounds how
    many WAIT.

    Completions drain one at a time at up to 300s each, so a cold catalogue
    of prolific authors enqueues faster than it empties, and every pending
    task retains its own copy of a partial ASIN list -- thousands of them is
    hundreds of megabytes per worker, six workers over. Depth is also
    latency: past the ceiling a queued completion would start over an hour
    late, spending the Audible lane on a request nobody is waiting for.
    persist_queue sheds for exactly this reason; this is the sibling that
    was missing it."""
    release = asyncio.Event()

    async def _walk(asin, region, session, *, time_budget, allow_background_completion):
        await release.wait()
        return AuthorBooksResult([], True)

    with patch("app.services.audible.authors._walk_author_books", new=_walk), \
         patch.object(completion, "_CompletionSession"):
        for i in range(completion._COMPLETION_QUEUE_MAX):
            completion.request_author_books_completion(f"B{i:09d}", "us", ["B0A"])
        await asyncio.sleep(0)
        assert completion.inflight_count() == completion._COMPLETION_QUEUE_MAX

        # One past the ceiling is refused, not queued.
        completion.request_author_books_completion("B999999999", "us", ["B0A"])
        assert completion.inflight_count() == completion._COMPLETION_QUEUE_MAX
        assert ("B999999999", "us") not in completion._completion_inflight

        release.set()
        await _drain()


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


@pytest.mark.asyncio
async def test_a_clean_walk_never_requests_completion():
    """
    The other side of the invariant every test above assumes: a walk that
    already finished clean must never be handed to
    request_author_books_completion at all -- if it were, the concurrency
    limit, the per-author dedupe, and the attempt cap this module maintains
    would all be spending budget re-walking authors that never needed it.

    Minimal mocking gets to a clean result without needing every wave: no
    author name to resolve means the catalog wave is never even scheduled
    (`_walk_author_books` only appends that task `if author_name`), so
    nothing needs mocking there at all. A screens walk that terminates
    COMPLETED and a DB backstop read that returns rather than fails are
    then enough to make screens_clean, catalog_clean (trivially, via the
    no-name case), and db_clean all true at once.
    """
    session = AsyncMock()
    screen_result = _ScreenBooksResult(
        asins=["B0CLEAN0001"],
        pages_fetched=1,
        product_count=1,
        invalid_skipped=0,
        attribution_rejected=0,
        termination_reason=SCREENS_REASON_COMPLETED,
    )

    with patch("app.services.audible.authors._resolve_author_name", new=AsyncMock(return_value=None)), \
         patch("app.services.audible.authors._fetch_author_books_by_screen", new=AsyncMock(return_value=screen_result)), \
         patch("app.services.audible.authors.get_author_book_asins_from_db", new=AsyncMock(return_value=[])), \
         patch("app.services.audible.authors.persist_author_books_cache_background"), \
         patch("app.services.audible.authors.request_author_books_completion") as mock_completion:
        result = await _walk_author_books("B000AUTHOR", "us", session)

    assert result.asins == ["B0CLEAN0001"]
    assert result.is_complete is True
    mock_completion.assert_not_called()
