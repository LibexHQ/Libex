"""
Chapter backfill classification tests.

Scoped narrowly to the classification logic this branch's fix turns on:
_PERMANENT_UPSTREAM_STATUSES, _is_backoff_signal, and _process_one's
(outcome, is_backoff_signal) pairs. Nothing here exercises the run loop,
the DB queue, or the pacing/pause machinery -- those are operational
concerns of a one-off script, not the content of this fix.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
from app.core.exceptions import AudibleAPIException, NotFoundException
from scripts.backfill_chapters import (
    _Outcome,
    _PERMANENT_UPSTREAM_STATUSES,
    _is_backoff_signal,
    _process_one,
)


# ============================================================
# _PERMANENT_UPSTREAM_STATUSES
# ============================================================

def test_permanent_upstream_statuses_contains_only_400():
    """Limited to exactly {400} -- the only status actually observed in
    production. Anything wider is a speculative expansion of what gets
    marked dead forever without evidence."""
    assert _PERMANENT_UPSTREAM_STATUSES == {400}


# ============================================================
# _is_backoff_signal
# ============================================================

@pytest.mark.parametrize("upstream_status", [None, 401, 403, 429, 500, 503, 599])
def test_is_backoff_signal_true_for_plausible_ip_trouble(upstream_status):
    """No response at all, 401/403, 429, and every 5xx all plausibly mean
    the exit IP itself is in trouble -- these must feed the back-off window."""
    assert _is_backoff_signal(upstream_status) is True


@pytest.mark.parametrize("upstream_status", [400, 404, 405, 418])
def test_is_backoff_signal_false_for_non_ip_statuses(upstream_status):
    """400 (confirmed-permanent), 404 (confirmed-terminal), and any status
    nobody anticipated (405, 418) must NOT feed the back-off window -- only
    the explicit set above plausibly indicates IP trouble."""
    assert _is_backoff_signal(upstream_status) is False


# ============================================================
# _process_one classification
# ============================================================

@pytest.mark.asyncio
async def test_process_one_400_is_permanent_and_marked_checked_not_backoff():
    """A confirmed-permanent 400 is terminal: PERMANENT outcome, marked
    checked so it leaves the queue for good, and does NOT feed the back-off
    window. This is the exact bug this branch fixes -- these ASINs were
    previously classified ERROR, never marked, and re-selected forever."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=AudibleAPIException("bad request", upstream_status=400)),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff_signal = await _process_one(session, "B00TEST0400", "us")

    assert outcome == _Outcome.PERMANENT
    assert is_backoff_signal is False
    mark_checked.assert_awaited_once_with(session, "B00TEST0400")


@pytest.mark.asyncio
async def test_process_one_404_is_not_found_and_marked_checked_not_backoff():
    """A 404 stays terminal exactly as before this fix: NOT_FOUND, marked
    checked, and never a back-off signal."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=NotFoundException()),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff_signal = await _process_one(session, "B00TEST0404", "us")

    assert outcome == _Outcome.NOT_FOUND
    assert is_backoff_signal is False
    mark_checked.assert_awaited_once_with(session, "B00TEST0404")


@pytest.mark.parametrize("upstream_status", [401, 403, 429, 500, 503])
@pytest.mark.asyncio
async def test_process_one_ip_trouble_statuses_are_error_and_feed_backoff_unmarked(upstream_status):
    """401/403/429/5xx are all ERROR (retried later -- never marked checked)
    and DO feed the back-off window, since each plausibly means the exit IP
    is in trouble rather than a fact about this specific ASIN."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=AudibleAPIException("upstream trouble", upstream_status=upstream_status)),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff_signal = await _process_one(session, "B00TESTSTAT", "us")

    assert outcome == _Outcome.ERROR
    assert is_backoff_signal is True
    mark_checked.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_one_none_upstream_status_is_error_and_feeds_backoff():
    """A timeout/connection failure arrives as AudibleAPIException with
    upstream_status None -- ERROR, unmarked, and DOES feed the back-off
    window. This is the load-bearing None case: it must not be confused
    with a confirmed-permanent per-ASIN fact."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=AudibleAPIException("timed out", upstream_status=None)),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff_signal = await _process_one(session, "B00TESTNONE", "us")

    assert outcome == _Outcome.ERROR
    assert is_backoff_signal is True
    mark_checked.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_one_unanticipated_status_is_error_not_permanent_and_no_backoff():
    """The safety property: a status nobody anticipated (405) is ERROR (so
    it's retried, never silently marked permanently dead) but does NOT feed
    the back-off window (since it isn't one of the plausible-IP-trouble
    statuses either). An explicit permanent set means unknowns default to
    'retry', never to 'give up on this ASIN forever'."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=AudibleAPIException("method not allowed", upstream_status=405)),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff_signal = await _process_one(session, "B00TEST0405", "us")

    assert outcome == _Outcome.ERROR
    assert is_backoff_signal is False
    mark_checked.assert_not_awaited()
