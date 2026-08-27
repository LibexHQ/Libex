"""
Service tests for the seeder's shed-awareness: whether an author, series, or
narrator gets stamped with last_seeded_at when the background persist queue
sheds a chunk of the books it just fetched for that entity.

Before this fix, _expand_authors/_expand_series/_expand_narrators stamped
their entity unconditionally after _fetch_and_persist, so a shed chunk left
books fetched from Audible but never written -- invisible for SEED_STALE_DAYS
with nothing left pointing back at them. The fix threads a bool up from
persist_queue's own admission decision (PersistOutcome) through
get_books_by_asins' persist_outcome out-parameter and _fetch_and_persist's
return value, and gates the stamp (and the entity's own processed counter)
on it.

Two layers are tested, deliberately kept apart:

- _expand_authors/_expand_series/_expand_narrators' own stamp/counter gating,
  with _fetch_and_persist mocked to a controlled True/False -- the same
  mocking boundary tests/services/test_seeder_new_releases.py already
  establishes for these functions. This is a forced, deterministic value,
  not a real backlog condition, and it is what proves the `if persisted:`
  branch in each function actually gates on the value it's given.
- _fetch_and_persist's own bool return, both from a mocked get_books_by_asins
  (proving the PersistOutcome.SHED-in-outcome check) and from a real forced
  shed against app.services.db.persist_queue's actual backlog state (the
  same technique tests/services/test_persist_queue.py uses -- setting
  _queued_books directly rather than generating 5000 real books -- proving
  the plumbing between persist_queue and this function, not just a mock of
  it). The real-forced test never reaches Postgres or the network: a shed
  batch returns out of persist_queue._spawn before any task is created, so
  nothing here ever touches _BackgroundSession.

A second, later fix closed a sibling gap in the same function: a chunk whose
fetch/persist call raised outright -- get_books_by_asins raises
NotFoundException when a chunk's ASINs are in neither Audible, the DB, nor
the cache -- used to leave all_admitted untouched (the per-chunk except was a
bare pass), so a raised chunk was stamped over exactly like an admitted one,
despite never reaching storage. The fix sets all_admitted = False in that
except, and the SHED check itself sits unconditionally after the try/except
(with outcome initialized above it, not inside the try) so a chunk that both
reports a shed and then raises still has that shed honored. Covered here at
both layers again: the three _expand_* raise-gating tests below (using the
real, unmocked _fetch_and_persist, since the raise has to travel through it
rather than being handed a controlled bool) and _fetch_and_persist's own
raise-path bool-return tests, mocking get_books_by_asins directly.
"""

# Standard library
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest

# Core
from app.core.exceptions import NotFoundException

# Services
from app.services import seeder
from app.services.db.persist_queue import PersistOutcome

_AUDIBLE_CLIENT_GET = "app.services.audible.client.audible_get"


def _hydration_product(asin):
    """Minimal product shape that survives normalization -- a real title and
    a non-placeholder publication_datetime. Mirrors the identically-named
    helper in tests/services/test_books_service.py."""
    return {
        "asin": asin, "title": f"Book {asin}", "authors": [], "narrators": [],
        "relationships": [], "product_images": {}, "category_ladders": [],
        "rating": {}, "publication_datetime": "2021-01-01T00:00:00Z",
    }


class _FakeSessionCM:
    """Minimal async context manager standing in for SessionFactory()'s
    `async with` usage, wrapping a single reusable mock session. Mirrors
    tests/services/test_seeder_new_releases.py's own helper of the same
    name."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _select_session(rows):
    """A session whose one execute() answers a select(...).fetchall() with
    the given rows -- the shape _expand_authors/_expand_series/
    _expand_narrators' own initial queries read."""
    result = MagicMock()
    result.fetchall.return_value = rows
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    return session


def _passthrough_session():
    """A session that answers rollback() and an empty execute().fetchall() --
    enough for get_books_by_asins' unconditional rollback and
    _gather_chapters' chapters_checked_at query to both no-op."""
    session = AsyncMock()
    session.rollback = AsyncMock()
    empty_result = MagicMock()
    empty_result.fetchall.return_value = []
    session.execute = AsyncMock(return_value=empty_result)
    return session


def _multi_call_session_factory(first_call_rows):
    """A SessionFactory side_effect for the three chunk-raises-through-
    _expand_* tests below, which call the real (unmocked) _fetch_and_persist
    -- unlike the SHED-gating tests above, SessionFactory is invoked more
    than once per test: once for the entity's own initial select, then again
    inside _fetch_and_persist's own SessionFactory() call, then again inside
    _gather_chapters'. Only the first call needs to answer with the entity
    row; every call after that only needs to no-op, which _passthrough_session
    already does for both get_books_by_asins' rollback and _gather_chapters'
    chapters_checked_at query."""
    calls = {"n": 0}

    def _factory():
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeSessionCM(_select_session(first_call_rows))
        return _FakeSessionCM(_passthrough_session())

    return _factory


# ============================================================
# _expand_authors -- STAMP AND COUNTER GATING
# ============================================================

@pytest.mark.asyncio
async def test_expand_authors_does_not_stamp_or_count_when_persist_is_shed():
    """A shed chunk of an author's new books must leave the author
    unstamped and out of authors_processed, so the next cycle retries it
    instead of the books going quiet for SEED_STALE_DAYS."""
    fake_authors = [(1, "B000AUTHOR1", "Frank Herbert")]

    with patch.object(seeder, "SessionFactory", return_value=_FakeSessionCM(_select_session(fake_authors))), \
         patch.object(seeder, "fetch_author_books_by_name", new=AsyncMock(return_value=(["B0BOOK0001"], 1))), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(return_value=["B0BOOK0001"])), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock(return_value=False)), \
         patch.object(seeder, "_stamp_author", new=AsyncMock()) as mock_stamp:
        stats = await seeder._expand_authors("us", delay=0)

    mock_stamp.assert_not_awaited()
    assert stats["authors_processed"] == 0
    assert stats["books_discovered"] == 1
    assert stats["errors"] == 0


@pytest.mark.asyncio
async def test_expand_authors_stamps_and_counts_when_persist_is_admitted():
    """The unshed case: without this fix ever regressing to 'never stamp
    anything', an admitted write must still stamp and count normally."""
    fake_authors = [(1, "B000AUTHOR1", "Frank Herbert")]

    with patch.object(seeder, "SessionFactory", return_value=_FakeSessionCM(_select_session(fake_authors))), \
         patch.object(seeder, "fetch_author_books_by_name", new=AsyncMock(return_value=(["B0BOOK0001"], 1))), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(return_value=["B0BOOK0001"])), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock(return_value=True)), \
         patch.object(seeder, "_stamp_author", new=AsyncMock()) as mock_stamp:
        stats = await seeder._expand_authors("us", delay=0)

    mock_stamp.assert_awaited_once_with(1)
    assert stats["authors_processed"] == 1
    assert stats["books_discovered"] == 1
    assert stats["errors"] == 0


@pytest.mark.asyncio
async def test_expand_authors_does_not_stamp_or_count_when_a_chunk_raises():
    """The sibling gap to the shed case above: a chunk whose fetch/persist
    call raised outright -- get_books_by_asins raises NotFoundException when
    a chunk's ASINs are in neither Audible, the DB, nor the cache -- must
    leave the author just as unstamped as a shed chunk does, since it
    persisted nothing either. Runs the real _fetch_and_persist (not mocked)
    so the raise actually travels through its except before reaching
    _expand_authors, rather than asserting a controlled bool it was handed."""
    fake_authors = [(1, "B000AUTHOR1", "Frank Herbert")]

    async def _raising_get_books(asins, region, session, persist_outcome=None):
        raise NotFoundException("Audible unavailable and no cached data found")

    with patch.object(seeder, "SessionFactory", side_effect=_multi_call_session_factory(fake_authors)), \
         patch.object(seeder, "fetch_author_books_by_name", new=AsyncMock(return_value=(["B0BOOK0001"], 1))), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(return_value=["B0BOOK0001"])), \
         patch.object(seeder, "get_books_by_asins", new=AsyncMock(side_effect=_raising_get_books)), \
         patch.object(seeder, "_stamp_author", new=AsyncMock()) as mock_stamp:
        stats = await seeder._expand_authors("us", delay=0)

    mock_stamp.assert_not_awaited()
    assert stats["authors_processed"] == 0
    assert stats["books_discovered"] == 1
    # The raise is absorbed by _fetch_and_persist's own except, not
    # re-raised -- it must never reach _expand_authors' own try/except, or
    # this would land in "errors" instead of correctly gating the stamp.
    assert stats["errors"] == 0


# ============================================================
# _expand_series -- STAMP AND COUNTER GATING
# ============================================================

def _series_relationships_response(series_asin, book_asin):
    return {"product": {"relationships": [
        {"asin": book_asin, "relationship_type": "product"},
    ]}}


@pytest.mark.asyncio
async def test_expand_series_does_not_stamp_or_count_when_persist_is_shed():
    """See test_expand_authors' identical case: a shed chunk must leave the
    series unstamped and out of series_processed."""
    series_asin = "B0SERIES01"
    book_asin = "B0SERBOOK1"

    with patch(_AUDIBLE_CLIENT_GET, new=AsyncMock(return_value=_series_relationships_response(series_asin, book_asin))), \
         patch.object(seeder, "SessionFactory", return_value=_FakeSessionCM(_select_session([(series_asin,)]))), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(return_value=[book_asin])), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock(return_value=False)), \
         patch.object(seeder, "_stamp_series", new=AsyncMock()) as mock_stamp:
        stats = await seeder._expand_series("us", delay=0)

    mock_stamp.assert_not_awaited()
    assert stats["series_processed"] == 0
    assert stats["books_discovered"] == 1
    assert stats["errors"] == 0


@pytest.mark.asyncio
async def test_expand_series_stamps_and_counts_when_persist_is_admitted():
    series_asin = "B0SERIES01"
    book_asin = "B0SERBOOK1"

    with patch(_AUDIBLE_CLIENT_GET, new=AsyncMock(return_value=_series_relationships_response(series_asin, book_asin))), \
         patch.object(seeder, "SessionFactory", return_value=_FakeSessionCM(_select_session([(series_asin,)]))), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(return_value=[book_asin])), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock(return_value=True)), \
         patch.object(seeder, "_stamp_series", new=AsyncMock()) as mock_stamp:
        stats = await seeder._expand_series("us", delay=0)

    mock_stamp.assert_awaited_once_with(series_asin)
    assert stats["series_processed"] == 1
    assert stats["books_discovered"] == 1
    assert stats["errors"] == 0


@pytest.mark.asyncio
async def test_expand_series_does_not_stamp_or_count_when_a_chunk_raises():
    """See test_expand_authors' identical raise case: a chunk that raised
    persisted nothing, so the series must be left unstamped exactly as a
    shed chunk would, via the real _fetch_and_persist."""
    series_asin = "B0SERIES01"
    book_asin = "B0SERBOOK1"

    async def _raising_get_books(asins, region, session, persist_outcome=None):
        raise NotFoundException("Audible unavailable and no cached data found")

    with patch(_AUDIBLE_CLIENT_GET, new=AsyncMock(return_value=_series_relationships_response(series_asin, book_asin))), \
         patch.object(seeder, "SessionFactory", side_effect=_multi_call_session_factory([(series_asin,)])), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(return_value=[book_asin])), \
         patch.object(seeder, "get_books_by_asins", new=AsyncMock(side_effect=_raising_get_books)), \
         patch.object(seeder, "_stamp_series", new=AsyncMock()) as mock_stamp:
        stats = await seeder._expand_series("us", delay=0)

    mock_stamp.assert_not_awaited()
    assert stats["series_processed"] == 0
    assert stats["books_discovered"] == 1
    assert stats["errors"] == 0


# ============================================================
# _expand_narrators -- STAMP AND COUNTER GATING
# ============================================================

def _narrator_products_response(narrator_name, book_asin):
    return {"products": [
        {"asin": book_asin, "narrators": [{"name": narrator_name}]},
    ]}


@pytest.mark.asyncio
async def test_expand_narrators_does_not_stamp_or_count_when_persist_is_shed():
    """See test_expand_authors' identical case: a shed chunk must leave the
    narrator unstamped and out of narrators_processed."""
    narrator_name = "Simon Vance"
    book_asin = "B0NARBOOK1"

    with patch(_AUDIBLE_CLIENT_GET, new=AsyncMock(return_value=_narrator_products_response(narrator_name, book_asin))), \
         patch.object(seeder, "SessionFactory", return_value=_FakeSessionCM(_select_session([(narrator_name,)]))), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(return_value=[book_asin])), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock(return_value=False)), \
         patch.object(seeder, "_stamp_narrator", new=AsyncMock()) as mock_stamp:
        stats = await seeder._expand_narrators("us", delay=0)

    mock_stamp.assert_not_awaited()
    assert stats["narrators_processed"] == 0
    assert stats["books_discovered"] == 1
    assert stats["errors"] == 0


@pytest.mark.asyncio
async def test_expand_narrators_stamps_and_counts_when_persist_is_admitted():
    narrator_name = "Simon Vance"
    book_asin = "B0NARBOOK1"

    with patch(_AUDIBLE_CLIENT_GET, new=AsyncMock(return_value=_narrator_products_response(narrator_name, book_asin))), \
         patch.object(seeder, "SessionFactory", return_value=_FakeSessionCM(_select_session([(narrator_name,)]))), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(return_value=[book_asin])), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock(return_value=True)), \
         patch.object(seeder, "_stamp_narrator", new=AsyncMock()) as mock_stamp:
        stats = await seeder._expand_narrators("us", delay=0)

    mock_stamp.assert_awaited_once_with(narrator_name)
    assert stats["narrators_processed"] == 1
    assert stats["books_discovered"] == 1
    assert stats["errors"] == 0


@pytest.mark.asyncio
async def test_expand_narrators_does_not_stamp_or_count_when_a_chunk_raises():
    """See test_expand_authors' identical raise case: a chunk that raised
    persisted nothing, so the narrator must be left unstamped exactly as a
    shed chunk would, via the real _fetch_and_persist."""
    narrator_name = "Simon Vance"
    book_asin = "B0NARBOOK1"

    async def _raising_get_books(asins, region, session, persist_outcome=None):
        raise NotFoundException("Audible unavailable and no cached data found")

    with patch(_AUDIBLE_CLIENT_GET, new=AsyncMock(return_value=_narrator_products_response(narrator_name, book_asin))), \
         patch.object(seeder, "SessionFactory", side_effect=_multi_call_session_factory([(narrator_name,)])), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(return_value=[book_asin])), \
         patch.object(seeder, "get_books_by_asins", new=AsyncMock(side_effect=_raising_get_books)), \
         patch.object(seeder, "_stamp_narrator", new=AsyncMock()) as mock_stamp:
        stats = await seeder._expand_narrators("us", delay=0)

    mock_stamp.assert_not_awaited()
    assert stats["narrators_processed"] == 0
    assert stats["books_discovered"] == 1
    assert stats["errors"] == 0


# ============================================================
# _fetch_and_persist -- BOOL RETURN, get_books_by_asins MOCKED
# ============================================================

@pytest.mark.asyncio
async def test_fetch_and_persist_returns_false_when_a_chunk_reports_shed():
    """PersistOutcome.SHED appearing anywhere in a chunk's outcome list must
    flip the whole call's return to False, not just that chunk's own local
    state."""
    async def _get_books(asins, region, session, persist_outcome=None):
        if persist_outcome is not None:
            persist_outcome.append(PersistOutcome.SHED)
        return []

    with patch.object(seeder, "SessionFactory", return_value=_FakeSessionCM(_passthrough_session())), \
         patch.object(seeder, "get_books_by_asins", new=AsyncMock(side_effect=_get_books)):
        result = await seeder._fetch_and_persist(["B0ONE00001"], "us", delay=0)

    assert result is False


@pytest.mark.asyncio
async def test_fetch_and_persist_returns_true_when_every_chunk_is_admitted():
    """Without this, the fix could regress to 'always return False' and
    every entity would go permanently unstamped."""
    async def _get_books(asins, region, session, persist_outcome=None):
        if persist_outcome is not None:
            persist_outcome.append(PersistOutcome.ADMITTED)
        return []

    with patch.object(seeder, "SessionFactory", return_value=_FakeSessionCM(_passthrough_session())), \
         patch.object(seeder, "get_books_by_asins", new=AsyncMock(side_effect=_get_books)):
        result = await seeder._fetch_and_persist(["B0ONE00001"], "us", delay=0)

    assert result is True


@pytest.mark.asyncio
async def test_fetch_and_persist_returns_false_if_any_of_several_chunks_sheds():
    """101 ASINs is three 50-wide chunks. Only the middle one sheds -- the
    call must still report False for the whole batch, since one shed chunk
    is one entity's books that never reached storage."""
    asins = [f"B0MANY{i:04d}" for i in range(101)]
    call_count = {"n": 0}

    async def _get_books(chunk, region, session, persist_outcome=None):
        call_count["n"] += 1
        if persist_outcome is not None:
            outcome = PersistOutcome.SHED if call_count["n"] == 2 else PersistOutcome.ADMITTED
            persist_outcome.append(outcome)
        return []

    with patch.object(seeder, "SessionFactory", return_value=_FakeSessionCM(_passthrough_session())), \
         patch.object(seeder, "get_books_by_asins", new=AsyncMock(side_effect=_get_books)):
        result = await seeder._fetch_and_persist(asins, "us", delay=0)

    assert call_count["n"] == 3
    assert result is False


@pytest.mark.asyncio
async def test_fetch_and_persist_returns_false_when_a_chunk_raises():
    """The sibling to the SHED test above, for the raise path the per-chunk
    except used to swallow with a bare pass: get_books_by_asins raising
    (the live trigger is NotFoundException, when a chunk's ASINs are in
    neither Audible, the DB, nor the cache) must flip the whole call's
    return to False just as surely as an explicit SHED does, since the
    chunk persisted nothing either way."""
    async def _get_books(asins, region, session, persist_outcome=None):
        raise NotFoundException("Audible unavailable and no cached data found")

    with patch.object(seeder, "SessionFactory", return_value=_FakeSessionCM(_passthrough_session())), \
         patch.object(seeder, "get_books_by_asins", new=AsyncMock(side_effect=_get_books)):
        result = await seeder._fetch_and_persist(["B0ONE00001"], "us", delay=0)

    assert result is False


@pytest.mark.asyncio
async def test_fetch_and_persist_still_returns_false_when_a_chunk_sheds_then_raises():
    """The reason the SHED check sits unconditionally after the try/except
    (with outcome initialized above the try) rather than inside it: a chunk
    can report SHED into its outcome list and then still raise before
    get_books_by_asins returns. If the check sat inside the try, a raise at
    that point could skip it, discarding a known shed; here it doesn't have
    the chance to, since the check runs regardless of what the try/except
    just resolved."""
    async def _get_books(asins, region, session, persist_outcome=None):
        if persist_outcome is not None:
            persist_outcome.append(PersistOutcome.SHED)
        raise NotFoundException("Audible unavailable and no cached data found")

    with patch.object(seeder, "SessionFactory", return_value=_FakeSessionCM(_passthrough_session())), \
         patch.object(seeder, "get_books_by_asins", new=AsyncMock(side_effect=_get_books)):
        result = await seeder._fetch_and_persist(["B0ONE00001"], "us", delay=0)

    assert result is False


# ============================================================
# _fetch_and_persist -- REAL FORCED SHED THROUGH persist_queue
# ============================================================
# Forces app.services.db.persist_queue's actual backlog to capacity, the
# same technique tests/services/test_persist_queue.py itself uses (setting
# _queued_books directly) rather than generating 5000 real books to wait
# for a natural shed. get_books_by_asins and persist_books_background are
# both the real functions here -- only SessionFactory (this module's own
# DB boundary) and Audible are stood in for, so this is the one test in
# this file that proves the wiring across seeder.py, books.py, and
# persist_queue.py, not just seeder.py's own conditional.

@pytest.mark.asyncio
async def test_fetch_and_persist_reports_false_under_a_real_forced_backlog_shed():
    """A shed batch returns out of persist_queue._spawn before any task is
    created, so this never reaches Postgres or the network -- see
    PersistOutcome's own docstring for why that's true by construction, not
    just by luck here."""
    import app.services.db.persist_queue as pq

    asin = "B0REALSHED"
    original_queued = pq._queued_books
    pq._queued_books = pq._PERSIST_BACKLOG_MAX_BOOKS
    try:
        with patch.object(seeder, "SessionFactory", return_value=_FakeSessionCM(_passthrough_session())), \
             patch(
                 "app.services.audible.books.audible_get",
                 new=AsyncMock(return_value={"product": _hydration_product(asin)}),
             ):
            result = await seeder._fetch_and_persist([asin], "us", delay=0)
    finally:
        pq._queued_books = original_queued
        pq._inflight.clear()

    assert result is False
    assert pq._inflight == set()


@pytest.mark.asyncio
async def test_fetch_and_persist_reports_true_when_the_real_backlog_has_room():
    """The sibling of the forced-shed test above: with the real backlog at
    its normal, empty state, an admitted write must still report True.
    persist_books_background is mocked here (not the backlog state) because
    an admitted call schedules a real background write, and this test must
    not let that write run against a real engine."""
    from app.services.db.persist_queue import PersistOutcome as _PO

    asin = "B0REALADMIT"

    with patch.object(seeder, "SessionFactory", return_value=_FakeSessionCM(_passthrough_session())), \
         patch(
             "app.services.audible.books.audible_get",
             new=AsyncMock(return_value={"product": _hydration_product(asin)}),
         ), \
         patch(
             "app.services.audible.books.persist_books_background",
             return_value=_PO.ADMITTED,
         ):
        result = await seeder._fetch_and_persist([asin], "us", delay=0)

    assert result is True
