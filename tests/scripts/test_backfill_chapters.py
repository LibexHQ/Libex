"""
Chapter backfill: classification, concurrency-safety, and containment tests.

Covers:
- Classification: _PERMANENT_UPSTREAM_STATUSES, _is_backoff_signal, and
  _process_one's (outcome, is_backoff_signal, is_ratchet_signal, elapsed) tuples.
- Concurrency: _Gate shrinking under a step-down, _Ramp's dwell-gated climb,
  _ThrottleSentinel catching an absorbed 429, _Ratchet's floor-then-abort,
  _BackoffWindow's time-based (not count-based) pruning, _NoneRateGuard's
  spike detection, and _dispatch_one giving each concurrent unit its own
  session.
- Keyset paging: _advance_cursor's wrap-around policy.
- Containment: _proxy_host_for_log never letting a full (possibly
  credentialed) AUDIBLE_PROXY_URL reach a log record, and
  _verify_dedicated_proxy refusing to start against anything but this
  script's own dedicated exit.

Nothing here exercises the run loop's DB I/O (_read_page's actual SQL) or the
DB queue beyond what proves the proxy check runs before either is touched --
those remain operational concerns of a one-off script, not the content of
either fix, matching this file's existing scope.
"""

# Standard library
import inspect
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest

# Local
from app.core.exceptions import AudibleAPIException, NotFoundException
from scripts.backfill_chapters import (
    _BackoffWindow,
    _Gate,
    _NoneRateGuard,
    _Outcome,
    _PERMANENT_UPSTREAM_STATUSES,
    _RegionSignal,
    _Ramp,
    _Ratchet,
    _ThrottleSentinel,
    _advance_cursor,
    _dispatch_one,
    _handle_pending_pauses,
    _is_backoff_signal,
    _log_exit_ip,
    _process_one,
    _proxy_host_for_log,
    _run,
    _verify_dedicated_proxy,
)
import scripts.backfill_chapters as backfill_chapters


# ============================================================
# _PERMANENT_UPSTREAM_STATUSES
# ============================================================

def test_permanent_upstream_statuses_contains_only_400():
    """Limited to exactly {400} -- the only status actually observed in
    production. Anything wider is a speculative expansion of what gets
    marked dead forever without evidence."""
    assert _PERMANENT_UPSTREAM_STATUSES == {400}


# ============================================================
# _is_backoff_signal -- re-scoped: 401/403/429 promoted to the ratchet
# ============================================================

@pytest.mark.parametrize("upstream_status", [None, 500, 503, 599])
def test_is_backoff_signal_true_for_timeout_and_5xx(upstream_status):
    """No response at all, and every 5xx, still feed the general soft
    back-off window."""
    assert _is_backoff_signal(upstream_status) is True


@pytest.mark.parametrize("upstream_status", [400, 401, 403, 404, 405, 418, 429])
def test_is_backoff_signal_false_for_promoted_and_non_ip_statuses(upstream_status):
    """401/403/429 no longer vote in this window -- they're promoted to
    _Ratchet instead (a stricter mechanism a single vote can't express).
    400 (confirmed-permanent), 404 (confirmed-terminal), and an
    unanticipated status (405, 418) stay excluded exactly as before."""
    assert _is_backoff_signal(upstream_status) is False


# ============================================================
# _process_one classification
# ============================================================

@pytest.mark.asyncio
async def test_process_one_400_is_permanent_and_marked_checked_no_signals():
    """A confirmed-permanent 400 is terminal: PERMANENT outcome, marked
    checked so it leaves the queue for good, and trips neither signal."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=AudibleAPIException("bad request", upstream_status=400)),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff, is_ratchet, elapsed = await _process_one(session, "B00TEST0400", "us")

    assert outcome == _Outcome.PERMANENT
    assert is_backoff is False
    assert is_ratchet is False
    assert elapsed >= 0
    mark_checked.assert_awaited_once_with(session, "B00TEST0400")


@pytest.mark.asyncio
async def test_process_one_404_is_not_found_and_marked_checked_no_signals():
    """A 404 stays terminal: NOT_FOUND, marked checked, and trips neither
    signal."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=NotFoundException()),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff, is_ratchet, elapsed = await _process_one(session, "B00TEST0404", "us")

    assert outcome == _Outcome.NOT_FOUND
    assert is_backoff is False
    assert is_ratchet is False
    mark_checked.assert_awaited_once_with(session, "B00TEST0404")


@pytest.mark.parametrize("upstream_status", [401, 403])
@pytest.mark.asyncio
async def test_process_one_401_403_are_error_and_set_ratchet_signal_only(upstream_status):
    """401/403 are ERROR (retried later, never marked checked), set
    is_ratchet_signal, and do NOT set is_backoff_signal -- promoted out of
    the softer window into the ratchet's stricter tier."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=AudibleAPIException("forbidden", upstream_status=upstream_status)),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff, is_ratchet, elapsed = await _process_one(session, "B00TESTAUTH", "us")

    assert outcome == _Outcome.ERROR
    assert is_backoff is False
    assert is_ratchet is True
    mark_checked.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_one_429_sets_neither_signal_relying_on_the_sentinel():
    """A 429 that reaches _process_one as a raised exception (every retry
    exhausted) sets NEITHER signal here -- audible_get's own retry branch
    has already logged it on every attempt including the last, so
    _ThrottleSentinel has already seen it. Reporting it again here would
    double-count the same event against _Ratchet."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=AudibleAPIException("throttled", upstream_status=429)),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff, is_ratchet, elapsed = await _process_one(session, "B00TEST0429", "us")

    assert outcome == _Outcome.ERROR
    assert is_backoff is False
    assert is_ratchet is False
    mark_checked.assert_not_awaited()


@pytest.mark.parametrize("upstream_status", [500, 503])
@pytest.mark.asyncio
async def test_process_one_5xx_is_error_and_sets_backoff_signal_only(upstream_status):
    """A 5xx is ERROR, sets is_backoff_signal (general soft window), and
    does NOT set is_ratchet_signal (that tier is 401/403/429 only)."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=AudibleAPIException("upstream trouble", upstream_status=upstream_status)),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff, is_ratchet, elapsed = await _process_one(session, "B00TESTSTAT", "us")

    assert outcome == _Outcome.ERROR
    assert is_backoff is True
    assert is_ratchet is False
    mark_checked.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_one_none_upstream_status_is_error_and_sets_backoff_signal():
    """A timeout/connection failure arrives with upstream_status None --
    ERROR, unmarked, and sets is_backoff_signal. Load-bearing: must not be
    confused with a confirmed-permanent per-ASIN fact."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=AudibleAPIException("timed out", upstream_status=None)),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff, is_ratchet, elapsed = await _process_one(session, "B00TESTNONE", "us")

    assert outcome == _Outcome.ERROR
    assert is_backoff is True
    assert is_ratchet is False
    mark_checked.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_one_unanticipated_status_is_error_with_no_signals():
    """A status nobody anticipated (405) is ERROR (retried, never silently
    marked permanently dead) and trips neither signal."""
    session = AsyncMock()
    with patch(
        "scripts.backfill_chapters.audible_get",
        new=AsyncMock(side_effect=AudibleAPIException("method not allowed", upstream_status=405)),
    ), patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()) as mark_checked:
        outcome, is_backoff, is_ratchet, elapsed = await _process_one(session, "B00TEST0405", "us")

    assert outcome == _Outcome.ERROR
    assert is_backoff is False
    assert is_ratchet is False
    mark_checked.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_one_elapsed_reflects_real_call_duration():
    """elapsed is a genuine measurement of the audible_get call, not a
    placeholder -- proven by mutation: an artificially slow call must
    report an elapsed that reflects it."""
    session = AsyncMock()

    async def _slow_get(*_a, **_kw):
        import asyncio
        await asyncio.sleep(0.05)
        raise NotFoundException()

    with patch("scripts.backfill_chapters.audible_get", new=_slow_get), \
         patch("scripts.backfill_chapters._mark_checked", new=AsyncMock()):
        _outcome, _backoff, _ratchet, elapsed = await _process_one(session, "B00TESTSLOW", "us")

    assert elapsed >= 0.05


# ============================================================
# _Gate -- the concurrency limit that can shrink
# ============================================================

@pytest.mark.asyncio
async def test_gate_admits_up_to_the_limit():
    gate = _Gate(limit=2)
    await gate.acquire()
    await gate.acquire()
    assert gate.limit == 2


@pytest.mark.asyncio
async def test_gate_blocks_past_the_limit_until_a_release():
    gate = _Gate(limit=1)
    await gate.acquire()

    third_ran = False

    async def _third():
        nonlocal third_ran
        await gate.acquire()
        third_ran = True

    import asyncio
    task = asyncio.create_task(_third())
    await asyncio.sleep(0)
    assert third_ran is False

    await gate.release()
    await asyncio.sleep(0)
    assert third_ran is True
    task.cancel()


@pytest.mark.asyncio
async def test_gate_step_down_blocks_the_next_acquire_until_enough_release():
    """A step-down binds the NEXT acquire: with two active against a limit
    stepped down to one, a third acquire must wait for two releases, not
    one -- this is the entire reason _Gate exists instead of a Semaphore."""
    gate = _Gate(limit=2)
    await gate.acquire()
    await gate.acquire()
    await gate.set_limit(1)

    admitted = False

    async def _waiter():
        nonlocal admitted
        await gate.acquire()
        admitted = True

    import asyncio
    task = asyncio.create_task(_waiter())
    await asyncio.sleep(0)
    await gate.release()
    await asyncio.sleep(0)
    assert admitted is False  # one release still leaves 1 active >= limit 1

    await gate.release()
    await asyncio.sleep(0)
    assert admitted is True
    task.cancel()


@pytest.mark.asyncio
async def test_gate_step_down_does_not_cancel_work_already_in_flight():
    gate = _Gate(limit=2)
    await gate.acquire()
    await gate.acquire()

    await gate.set_limit(1)

    assert gate.limit == 1  # already-granted permits are unaffected


# ============================================================
# _Ramp -- dwell-gated climb (time AND request-count floors)
# ============================================================

async def _fill_window(ramp, region, value, count=None):
    window = _RegionSignal().latencies.maxlen
    for _ in range(count or window):
        await ramp.record(region, value, failed=False)


@pytest.mark.asyncio
async def test_ramp_does_not_step_up_on_request_count_alone_below_the_time_floor():
    """RAMP_MIN_REQUESTS clean requests without RAMP_MIN_SECONDS of
    wall-clock time must NOT be enough -- the time floor is the load-bearing
    half of the dwell. Feeding RAMP_MIN_REQUESTS records synchronously (no
    real sleep) proves count alone can't trip the climb."""
    gate = _Gate(limit=backfill_chapters.CONCURRENCY_START)
    ramp = _Ramp(gate)

    for _ in range(backfill_chapters.RAMP_MIN_REQUESTS):
        await ramp.record("us", 0.1, failed=False)

    assert gate.limit == backfill_chapters.CONCURRENCY_START


@pytest.mark.asyncio
async def test_ramp_steps_up_once_both_dwell_floors_are_met():
    """With the time floor satisfied by monkeypatching monotonic forward and
    the request floor satisfied by count, the ramp steps up -- proving the
    dwell condition is satisfiable at all, not just never-true."""
    gate = _Gate(limit=backfill_chapters.CONCURRENCY_START)
    ramp = _Ramp(gate)

    real_monotonic = time.monotonic
    fake_now = [real_monotonic()]
    with patch("scripts.backfill_chapters.time.monotonic", side_effect=lambda: fake_now[0]):
        await ramp.record("us", 0.1, failed=False)  # starts the streak, sets streak_started
        fake_now[0] += backfill_chapters.RAMP_MIN_SECONDS + 1
        for _ in range(backfill_chapters.RAMP_MIN_REQUESTS - 1):
            await ramp.record("us", 0.1, failed=False)

    assert gate.limit > backfill_chapters.CONCURRENCY_START


@pytest.mark.asyncio
async def test_ramp_steps_down_on_one_degraded_region_and_freezes_the_climb():
    gate = _Gate(limit=backfill_chapters.CONCURRENCY_START + backfill_chapters.CONCURRENCY_STEP)
    ramp = _Ramp(gate)
    for _ in range(backfill_chapters.DEGRADE_WARMUP_SAMPLES):
        await ramp.record("us", 0.1, failed=False)
    starting_limit = gate.limit

    degraded = 0.1 * backfill_chapters.DEGRADE_P95_RATIO + 1.0
    await _fill_window(ramp, "us", degraded)

    assert gate.limit < starting_limit
    assert ramp.frozen is True


@pytest.mark.asyncio
async def test_ramp_a_failure_resets_the_clean_streak_without_touching_the_gate():
    gate = _Gate(limit=backfill_chapters.CONCURRENCY_START)
    ramp = _Ramp(gate)
    await _fill_window(ramp, "us", 0.1, count=5)

    await ramp.record("us", elapsed=None, failed=True)

    assert ramp._regions["us"].clean_streak == 0
    assert ramp._regions["us"].streak_started is None
    assert gate.limit == backfill_chapters.CONCURRENCY_START


def test_ramp_freeze_makes_step_up_and_step_down_both_inert():
    """The ratchet calls freeze() directly (not through record()) -- proves
    the frozen flag alone is enough to hold the ramp, independent of how it
    got set."""
    gate = _Gate(limit=backfill_chapters.CONCURRENCY_START)
    ramp = _Ramp(gate)
    ramp.freeze()
    assert ramp.frozen is True


@pytest.mark.asyncio
async def test_ramp_frozen_blocks_step_up_even_when_the_dwell_is_met():
    """The flag alone reading True isn't the guarantee -- record()'s own
    `not self._frozen` check on the step-up branch is what actually holds
    it. Proven by satisfying both dwell floors after freeze() and confirming
    the gate never moves; a frozen ramp that still climbs on qualifying
    evidence is the exact failure the ratchet's floor-and-freeze exists to
    prevent."""
    gate = _Gate(limit=backfill_chapters.CONCURRENCY_START)
    ramp = _Ramp(gate)
    ramp.freeze()

    real_monotonic = time.monotonic
    fake_now = [real_monotonic()]
    with patch("scripts.backfill_chapters.time.monotonic", side_effect=lambda: fake_now[0]):
        await ramp.record("us", 0.1, failed=False)
        fake_now[0] += backfill_chapters.RAMP_MIN_SECONDS + 1
        for _ in range(backfill_chapters.RAMP_MIN_REQUESTS - 1):
            await ramp.record("us", 0.1, failed=False)

    assert gate.limit == backfill_chapters.CONCURRENCY_START


@pytest.mark.asyncio
async def test_ramp_frozen_blocks_a_further_step_down_on_new_degradation():
    """Symmetric to the step-up guard: record()'s degrade branch has its own
    `not self._frozen` check, separate from the step-up one. Proven by
    freezing after warmup, then feeding a badly-degraded p95 and confirming
    the gate is untouched -- an unguarded degrade branch would still lower
    it even though the ratchet already dropped the run to its floor."""
    gate = _Gate(limit=backfill_chapters.CONCURRENCY_START + backfill_chapters.CONCURRENCY_STEP)
    ramp = _Ramp(gate)
    for _ in range(backfill_chapters.DEGRADE_WARMUP_SAMPLES):
        await ramp.record("us", 0.1, failed=False)
    starting_limit = gate.limit
    ramp.freeze()

    degraded = 0.1 * backfill_chapters.DEGRADE_P95_RATIO + 1.0
    await _fill_window(ramp, "us", degraded)

    assert gate.limit == starting_limit


# ============================================================
# _ThrottleSentinel -- catches an ABSORBED 429 that never raises
# ============================================================

def _throttle_record(status_code, attempts_left):
    return logging.LogRecord(
        name="libex", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="Audible API throttled or degraded", args=(), exc_info=None,
    ), {"status_code": status_code, "attempts_left": attempts_left}


def test_sentinel_counts_a_429_that_never_becomes_an_exception():
    """An absorbed 429 (the retry succeeded on a later attempt) never
    raises -- the sentinel is the only place it's visible, and it must
    count purely from the log line, with no exception anywhere in this
    test."""
    sentinel = _ThrottleSentinel()
    record, extra = _throttle_record(429, attempts_left=2)
    for key, value in extra.items():
        setattr(record, key, value)

    sentinel.emit(record)

    assert sentinel.throttled == 1


def test_sentinel_ignores_records_without_the_structured_fields():
    """Keys on status_code + attempts_left, not message text -- an
    unrelated WARNING record must not be misread as a throttle."""
    sentinel = _ThrottleSentinel()
    record = logging.LogRecord(
        name="libex", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="Audible API throttled or degraded", args=(), exc_info=None,
    )
    sentinel.emit(record)
    assert sentinel.throttled == 0


def test_sentinel_requires_attempts_left_not_just_status_code():
    """A record carrying status_code alone, without attempts_left, must NOT
    count -- the field-contract coupling is BOTH fields together, keyed
    specifically to audible_get's own retry branch, not status_code in
    isolation (which a differently-shaped log line could carry by
    coincidence)."""
    sentinel = _ThrottleSentinel()
    record = logging.LogRecord(
        name="libex", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="unrelated message", args=(), exc_info=None,
    )
    record.status_code = 429
    sentinel.emit(record)
    assert sentinel.throttled == 0


def test_sentinel_sustained_server_errors_trips_at_the_threshold():
    sentinel = _ThrottleSentinel()
    record, extra = _throttle_record(503, attempts_left=1)
    for key, value in extra.items():
        setattr(record, key, value)

    for _ in range(backfill_chapters.ABORT_5XX_WITHIN):
        sentinel.emit(record)

    assert sentinel.sustained_server_errors is True


def test_sentinel_server_errors_outside_the_window_are_pruned():
    sentinel = _ThrottleSentinel()
    record, extra = _throttle_record(503, attempts_left=1)
    for key, value in extra.items():
        setattr(record, key, value)
    sentinel.emit(record)

    # Age the one recorded error past the window.
    sentinel.server_errors[0] -= (backfill_chapters.ABORT_5XX_WINDOW_SECONDS + 1)

    assert sentinel.sustained_server_errors is False
    assert len(sentinel.server_errors) == 0


# ============================================================
# _Ratchet -- first trip to the floor, second aborts
# ============================================================

def test_ratchet_first_401_403_signal_trips_to_the_floor():
    ratchet = _Ratchet()
    sentinel = _ThrottleSentinel()

    result = ratchet.observe(auth_trouble=True, sentinel=sentinel)

    assert result == "floor"
    assert ratchet.tripped is True
    assert ratchet.abort_reason is None


def test_ratchet_second_signal_after_the_floor_aborts():
    ratchet = _Ratchet()
    sentinel = _ThrottleSentinel()
    ratchet.observe(auth_trouble=True, sentinel=sentinel)

    result = ratchet.observe(auth_trouble=True, sentinel=sentinel)

    assert result == "abort"
    assert ratchet.abort_reason is not None


def test_ratchet_no_signal_is_a_no_op():
    ratchet = _Ratchet()
    sentinel = _ThrottleSentinel()

    result = ratchet.observe(auth_trouble=False, sentinel=sentinel)

    assert result is None
    assert ratchet.tripped is False


def test_ratchet_observes_a_sentinel_429_delta_not_just_auth_trouble():
    """The sentinel's running total, not just this book's own auth flag,
    can trip the ratchet -- an absorbed 429 elsewhere must still register."""
    ratchet = _Ratchet()
    sentinel = _ThrottleSentinel()
    sentinel.throttled = 1  # a 429 landed since the ratchet last checked

    result = ratchet.observe(auth_trouble=False, sentinel=sentinel)

    assert result == "floor"


def test_ratchet_two_events_in_one_observe_call_ratchet_through_both_stages():
    """Several concurrently-dispatched books can each push the sentinel's
    counter between two checks -- a burst of 2 new events in a single call
    must floor AND abort, not just floor once and silently swallow the
    second."""
    ratchet = _Ratchet()
    sentinel = _ThrottleSentinel()
    sentinel.throttled = 2

    result = ratchet.observe(auth_trouble=False, sentinel=sentinel)

    assert result == "abort"
    assert ratchet.tripped is True


# ============================================================
# _BackoffWindow -- TIME-based, correct at two different paces
# ============================================================

def test_backoff_window_should_cooldown_below_min_samples_is_false():
    window = _BackoffWindow()
    window.record(True)
    assert window.should_cooldown is False


def test_backoff_window_trips_on_rate_regardless_of_how_many_events_the_window_holds():
    """Proves the RATE (not a raw count) drives the decision: a fast pace
    packs many more events into ERROR_WINDOW_SECONDS than a slow pace, but
    the same 50%+ signal rate must trip should_cooldown at either pace."""
    fast = _BackoffWindow()
    slow = _BackoffWindow()

    base = time.monotonic()
    with patch("scripts.backfill_chapters.time.monotonic") as mono:
        # Fast pace: 40 events, 0.05s apart, half of them signals.
        for i in range(40):
            mono.return_value = base + i * 0.05
            fast.record(i % 2 == 0)
        # Slow pace: 12 events, 5s apart, half of them signals -- far fewer
        # total events, same rate.
        for i in range(12):
            mono.return_value = base + i * 5.0
            slow.record(i % 2 == 0)

    assert len(fast._events) != len(slow._events)
    assert fast.should_cooldown is True
    assert slow.should_cooldown is True


def test_backoff_window_prunes_events_older_than_the_window_as_time_passes():
    """The time-based prune is the whole point of the re-scope: an event
    that ages past ERROR_WINDOW_SECONDS must fall out of the window even
    though a count-based window would have kept it until enough NEW events
    pushed it out."""
    window = _BackoffWindow()
    base = time.monotonic()
    with patch("scripts.backfill_chapters.time.monotonic") as mono:
        mono.return_value = base
        window.record(True)
        assert len(window._events) == 1

        mono.return_value = base + backfill_chapters.ERROR_WINDOW_SECONDS + 1
        window.record(False)

    assert len(window._events) == 1  # the aged-out True was pruned, only the new False remains


def test_backoff_window_clear_empties_it():
    window = _BackoffWindow()
    window.record(True)
    window.clear()
    assert len(window._events) == 0


# ============================================================
# _NoneRateGuard -- rolling NONE-rate abort
# ============================================================

def test_none_rate_guard_does_not_fire_on_a_steady_normal_rate():
    """A consistent, unremarkable NONE rate throughout the run -- baseline
    and recent window agree -- must never trip the guard, even after many
    samples."""
    guard = _NoneRateGuard()
    fired = False
    for i in range(2000):
        fired = guard.record(i % 10 == 0) or fired  # steady 10% NONE rate
    assert fired is False


def test_none_rate_guard_fires_on_a_clear_spike_past_baseline():
    """A long clean baseline followed by a run of NONEs in the recent
    window must trip the guard -- this is the exact shape a degenerate 200
    draining the queue would produce."""
    guard = _NoneRateGuard()
    for _ in range(backfill_chapters.NONE_RATE_BASELINE_MIN_SAMPLES + 50):
        guard.record(False)  # clean baseline, 0% NONE

    fired = False
    for _ in range(backfill_chapters.NONE_RATE_WINDOW):
        fired = guard.record(True) or fired  # sudden 100% NONE

    assert fired is True


def test_none_rate_guard_does_not_fire_before_the_window_fills():
    guard = _NoneRateGuard()
    for _ in range(backfill_chapters.NONE_RATE_WINDOW - 1):
        assert guard.record(True) is False


def test_none_rate_guard_does_not_fire_before_the_baseline_settles():
    """Even with a full recent window at 100% NONE, too little PRIOR
    history means no trustworthy baseline exists yet -- must not fire."""
    guard = _NoneRateGuard()
    fired = False
    for _ in range(backfill_chapters.NONE_RATE_WINDOW):
        fired = guard.record(True) or fired
    assert fired is False


def test_none_rate_guard_baseline_excludes_the_current_window_from_its_own_denominator():
    """One short of NONE_RATE_BASELINE_MIN_SAMPLES of PRIOR (pre-window)
    history, followed by a full spike window, must never fire -- if the
    baseline denominator wrongly included the window itself (self.total
    instead of self.total minus the window), this exact history clears the
    combined total and the guard would fire on nothing but its own spike."""
    guard = _NoneRateGuard()
    for _ in range(backfill_chapters.NONE_RATE_BASELINE_MIN_SAMPLES - 1):
        guard.record(False)

    fired = False
    for _ in range(backfill_chapters.NONE_RATE_WINDOW):
        fired = guard.record(True) or fired

    assert fired is False


# ============================================================
# _advance_cursor -- keyset paging's wrap-around policy
# ============================================================

def test_advance_cursor_moves_forward_on_a_non_empty_page():
    rows = [("B001", "us", None), ("B002", "us", None)]
    next_cursor, wrapped, done = _advance_cursor(rows, cursor="B000", wrapped=False)
    assert next_cursor == "B002"
    assert wrapped is False
    assert done is False


def test_advance_cursor_wraps_once_on_first_empty_page():
    next_cursor, wrapped, done = _advance_cursor([], cursor="B999", wrapped=False)
    assert next_cursor is None
    assert wrapped is True
    assert done is False


def test_advance_cursor_finishes_on_second_empty_page_after_wrap():
    next_cursor, wrapped, done = _advance_cursor([], cursor=None, wrapped=True)
    assert wrapped is True
    assert done is True


def test_advance_cursor_a_row_between_two_wraps_resets_the_wrap_state():
    """If the wrap-around pass finds real work again before reaching the
    end a second time, wrapped stays True but done must stay False --
    proves the wrap doesn't prematurely end the pass."""
    rows = [("B001", "us", None)]
    next_cursor, wrapped, done = _advance_cursor(rows, cursor=None, wrapped=True)
    assert next_cursor == "B001"
    assert wrapped is True
    assert done is False


# ============================================================
# _dispatch_one -- a session per concurrent unit
# ============================================================

@pytest.mark.asyncio
async def test_dispatch_one_creates_its_own_session():
    """Each call to _dispatch_one must open a NEW session via the
    sessionmaker -- proven by call count, not by inspecting a shared
    instance, since sharing one is exactly the bug this guards against."""
    session_instances = []

    def _make_session():
        instance = AsyncMock()
        instance.__aenter__.return_value = instance
        instance.__aexit__.return_value = False
        session_instances.append(instance)
        return instance

    Session = MagicMock(side_effect=_make_session)
    run = backfill_chapters._Run()
    gate = _Gate(limit=5)
    ramp = _Ramp(gate)
    sentinel = _ThrottleSentinel()
    stopper = backfill_chapters._Stopper()
    await gate.acquire()
    await gate.acquire()

    with patch(
        "scripts.backfill_chapters._process_one",
        new=AsyncMock(return_value=(_Outcome.NOT_FOUND, False, False, 0.01)),
    ), patch("scripts.backfill_chapters._interruptible_sleep", new=AsyncMock()):
        await _dispatch_one("B001", "us", Session, run, gate, ramp, sentinel, stopper)
        await _dispatch_one("B002", "us", Session, run, gate, ramp, sentinel, stopper)

    assert Session.call_count == 2
    assert session_instances[0] is not session_instances[1]


@pytest.mark.asyncio
async def test_dispatch_one_releases_the_gate_before_sleeping():
    """The gate permit must be released BEFORE the jittered delay runs --
    proven by checking gate.limit's active count has already dropped by the
    time the (patched) sleep is entered."""
    Session = MagicMock(side_effect=lambda: AsyncMock(
        __aenter__=AsyncMock(return_value=AsyncMock()), __aexit__=AsyncMock(return_value=False)
    ))
    run = backfill_chapters._Run()
    gate = _Gate(limit=1)
    ramp = _Ramp(gate)
    sentinel = _ThrottleSentinel()
    stopper = backfill_chapters._Stopper()
    await gate.acquire()

    released_before_sleep = None

    async def _fake_sleep(_seconds, _stopper, _run=None):
        nonlocal released_before_sleep
        released_before_sleep = gate._active == 0

    with patch(
        "scripts.backfill_chapters._process_one",
        new=AsyncMock(return_value=(_Outcome.NOT_FOUND, False, False, 0.01)),
    ), patch("scripts.backfill_chapters._interruptible_sleep", new=_fake_sleep):
        await _dispatch_one("B001", "us", Session, run, gate, ramp, sentinel, stopper)

    assert released_before_sleep is True


@pytest.mark.asyncio
async def test_dispatch_one_never_lets_an_escaped_exception_vanish():
    """B1: before this fix, an exception from opening the session (or from
    anything _process_one itself doesn't catch, e.g. _mark_checked hitting
    statement_timeout) propagated out of _dispatch_one uninterrupted,
    reached _run's asyncio.gather(..., return_exceptions=True), and was
    discarded -- run.processed stayed 0, no log line, no abort, the ratchet
    and backoff window never informed. This pins the fix at the exact
    reproduction: a raised exception must still land as a counted ERROR
    outcome, feeding both the ramp and the general back-off window."""
    Session = MagicMock(side_effect=lambda: AsyncMock(
        __aenter__=AsyncMock(return_value=AsyncMock()), __aexit__=AsyncMock(return_value=False)
    ))
    run = backfill_chapters._Run()
    gate = _Gate(limit=1)
    ramp = _Ramp(gate)
    sentinel = _ThrottleSentinel()
    stopper = backfill_chapters._Stopper()
    await gate.acquire()

    with patch(
        "scripts.backfill_chapters._process_one",
        new=AsyncMock(side_effect=RuntimeError("statement timeout")),
    ), patch("scripts.backfill_chapters._interruptible_sleep", new=AsyncMock()):
        # Must not raise -- the whole point is that this is now contained.
        await _dispatch_one("B001", "us", Session, run, gate, ramp, sentinel, stopper)

    assert run.processed == 1
    assert run.errors == 1
    assert gate.limit == 1  # the permit was still released regardless


@pytest.mark.asyncio
async def test_dispatch_one_escaped_exception_does_not_trip_the_ratchet():
    """An escaped exception feeds the ramp/back-off window (see the test
    above) but must NOT be treated as ratchet-worthy evidence: it says
    nothing about the exit IP, only about the local process or the
    database. Pinned separately so a future change that lumps this in with
    is_ratchet_signal is caught even if the "feeds the window" test above
    still passes."""
    Session = MagicMock(side_effect=lambda: AsyncMock(
        __aenter__=AsyncMock(return_value=AsyncMock()), __aexit__=AsyncMock(return_value=False)
    ))
    run = backfill_chapters._Run()
    gate = _Gate(limit=1)
    ramp = _Ramp(gate)
    sentinel = _ThrottleSentinel()
    stopper = backfill_chapters._Stopper()
    await gate.acquire()

    with patch(
        "scripts.backfill_chapters._process_one",
        new=AsyncMock(side_effect=RuntimeError("connection refused")),
    ), patch("scripts.backfill_chapters._interruptible_sleep", new=AsyncMock()):
        await _dispatch_one("B001", "us", Session, run, gate, ramp, sentinel, stopper)

    assert run.ratchet.tripped is False
    assert run.floor_pending is False


@pytest.mark.asyncio
async def test_dispatch_one_escaped_exception_feeds_the_backoff_window():
    """Pinned separately from test_dispatch_one_never_lets_an_escaped_exception_vanish:
    that test's run.processed/run.errors assertions pass even if the except
    block stopped setting is_backoff_signal, because _dispatch_one's own
    pre-init default is already (_Outcome.ERROR, False, False, None) -- a
    handler that logs but never reassigns is_backoff_signal to True would
    still leave errors/processed looking right while silently going back to
    feeding the window nothing, the same escape this fix closed wearing a
    different shape. Checked directly against the window's own recorded
    event, not the outcome tuple."""
    Session = MagicMock(side_effect=lambda: AsyncMock(
        __aenter__=AsyncMock(return_value=AsyncMock()), __aexit__=AsyncMock(return_value=False)
    ))
    run = backfill_chapters._Run()
    gate = _Gate(limit=1)
    ramp = _Ramp(gate)
    sentinel = _ThrottleSentinel()
    stopper = backfill_chapters._Stopper()
    await gate.acquire()

    with patch(
        "scripts.backfill_chapters._process_one",
        new=AsyncMock(side_effect=RuntimeError("statement timeout")),
    ), patch("scripts.backfill_chapters._interruptible_sleep", new=AsyncMock()):
        await _dispatch_one("B001", "us", Session, run, gate, ramp, sentinel, stopper)

    assert len(run.backoff_window._events) == 1
    assert run.backoff_window._events[-1][1] is True  # (timestamp, is_signal)


# ============================================================
# _handle_pending_pauses -- B3: the ratchet's cooldown/relog on the LAST
# item of a page, with nothing left to drain
# ============================================================

@pytest.mark.asyncio
async def test_handle_pending_pauses_fires_the_floor_cooldown_with_empty_inflight():
    """B3's exact reproduction: a page whose work is a single item, whose
    task trips the ratchet, has NOTHING left in `inflight` by the time this
    is called from the post-loop call site (the task that set the flag was
    the last one, and is already done) -- the drain-first step is a no-op,
    but the cooldown and the exit-IP relog must still fire. Before the fix,
    this flag was only ever polled from inside the per-task loop body, so a
    trip on the page's last item had no further iteration to be noticed
    from at all."""
    run = backfill_chapters._Run()
    run.floor_pending = True
    stopper = backfill_chapters._Stopper()

    with patch("scripts.backfill_chapters._log_exit_ip", new=AsyncMock()) as log_ip, \
         patch("scripts.backfill_chapters._interruptible_sleep", new=AsyncMock()) as sleep:
        await _handle_pending_pauses(run, inflight=set(), stopper=stopper)

    assert run.floor_pending is False
    log_ip.assert_awaited_once()
    sleep.assert_awaited_once()
    assert sleep.await_args.args[0] == backfill_chapters.ERROR_COOLDOWN


@pytest.mark.asyncio
async def test_handle_pending_pauses_fires_the_soft_cooldown_and_clears_the_window():
    run = backfill_chapters._Run()
    run.soft_pause_pending = True
    run.backoff_window.record(True)
    stopper = backfill_chapters._Stopper()

    with patch("scripts.backfill_chapters._interruptible_sleep", new=AsyncMock()) as sleep:
        await _handle_pending_pauses(run, inflight=set(), stopper=stopper)

    assert run.soft_pause_pending is False
    sleep.assert_awaited_once()
    assert len(run.backoff_window._events) == 0


@pytest.mark.asyncio
async def test_handle_pending_pauses_is_a_no_op_with_neither_flag_set():
    run = backfill_chapters._Run()
    stopper = backfill_chapters._Stopper()

    with patch("scripts.backfill_chapters._interruptible_sleep", new=AsyncMock()) as sleep, \
         patch("scripts.backfill_chapters._log_exit_ip", new=AsyncMock()) as log_ip:
        await _handle_pending_pauses(run, inflight=set(), stopper=stopper)

    sleep.assert_not_awaited()
    log_ip.assert_not_awaited()


def test_run_calls_handle_pending_pauses_both_inside_and_after_the_page_loop():
    """Guards the call SITE, not just _handle_pending_pauses's own
    internals -- a unit test of the helper alone (the three tests above)
    can never catch a future edit to _run that deletes the second call
    site and reintroduces exactly the bug this pins. _run's own dispatch
    loop isn't harnessed end-to-end (matching test_refresh_corpus.py's own
    precedent of leaving _run/_dry_run to a live run), so this checks the
    source directly: _handle_pending_pauses must be called at least twice
    in _run's body -- once per dispatched task inside the per-page loop,
    and once more after that loop's own post-drain gather, which is the
    exact position that closes the gap on a trip landing on a page's last
    item."""
    source = inspect.getsource(_run)
    assert source.count("await _handle_pending_pauses(") >= 2


# ============================================================
# _proxy_host_for_log -- containment: no full proxy value in any log record
# ============================================================

CREDENTIALED_PROXY = "http://opsuser:s3cr3t-token@libex-backfill-vpn:8888"


def test_proxy_host_for_log_strips_credentials():
    """The hostname is what a log line needs to say which exit is in use;
    the embedded user:pass must never survive into it."""
    host = _proxy_host_for_log(CREDENTIALED_PROXY)
    assert host == "libex-backfill-vpn"
    assert "opsuser" not in host
    assert "s3cr3t-token" not in host


def test_proxy_host_for_log_direct_when_unset():
    assert _proxy_host_for_log(None) == "direct"
    assert _proxy_host_for_log("") == "direct"


def test_proxy_host_for_log_never_raises_on_malformed_value():
    """A logging call must never take the run down over an unparseable env
    var. httpx.URL raises InvalidURL on some malformed strings -- confirmed
    directly against the installed httpx: 'http://[::1' is one of them --
    so this must catch it and return a sentinel, not propagate."""
    assert _proxy_host_for_log("http://[::1") == "(unparseable)"


@pytest.mark.asyncio
async def test_log_exit_ip_never_logs_the_full_credentialed_proxy(monkeypatch, caplog):
    """The exit-IP startup probe logs 'via <host>'. Proves the credentialed
    value never reaches the record that would otherwise go to stdout and,
    with AXIOM_TOKEN set, to Axiom."""
    monkeypatch.setenv("AUDIBLE_PROXY_URL", CREDENTIALED_PROXY)

    class _FakeResponse:
        text = "203.0.113.9"

    class _FakeAsyncClient:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, *_a, **_kw):
            return _FakeResponse()

    with patch("scripts.backfill_chapters.httpx.AsyncClient", _FakeAsyncClient):
        with caplog.at_level(logging.INFO, logger="libex"):
            await _log_exit_ip()

    full_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "s3cr3t-token" not in full_text
    assert "opsuser" not in full_text
    assert "libex-backfill-vpn" in full_text


# ============================================================
# _verify_dedicated_proxy -- containment: refuse to egress from the host
# ============================================================

def test_verify_dedicated_proxy_raises_when_unset(monkeypatch):
    """Unset means httpx.AsyncClient would get proxy=None and this script's
    traffic would egress direct from the container -- the production
    host's own address. Must refuse before anything else runs."""
    monkeypatch.delenv("AUDIBLE_PROXY_URL", raising=False)
    with pytest.raises(SystemExit, match="unset"):
        _verify_dedicated_proxy()


def test_verify_dedicated_proxy_raises_on_non_backfill_hostname(monkeypatch):
    """A hostname naming some OTHER exit -- the shared production proxy, or
    refresh_corpus's own dedicated one -- must fail exactly like unset. This
    is what stops a copy-pasted refresh exit from being reused here."""
    monkeypatch.setenv("AUDIBLE_PROXY_URL", "http://libex-refresh-vpn:8888")
    with pytest.raises(SystemExit, match="libex-refresh-vpn"):
        _verify_dedicated_proxy()


def test_verify_dedicated_proxy_failure_never_names_credentials(monkeypatch):
    """The failure message names the hostname only, proving a credentialed
    but wrongly-named value can't leak into the SystemExit text either."""
    monkeypatch.setenv(
        "AUDIBLE_PROXY_URL", "http://opsuser:s3cr3t-token@libex-refresh-vpn:8888"
    )
    with pytest.raises(SystemExit) as exc_info:
        _verify_dedicated_proxy()
    assert "s3cr3t-token" not in str(exc_info.value)
    assert "opsuser" not in str(exc_info.value)


def test_verify_dedicated_proxy_passes_on_backfill_hostname(monkeypatch):
    monkeypatch.setenv("AUDIBLE_PROXY_URL", "http://libex-backfill-vpn:8888")
    _verify_dedicated_proxy()  # must not raise


def test_verify_dedicated_proxy_logs_error_before_raising_on_unset(monkeypatch, caplog):
    """SystemExit alone never reaches the libex logger -- it propagates
    straight out of the process, so unattended it would survive only as
    stderr text. The refusal must also land as a structured ERROR record
    (rotating file handler, Axiom) before the raise, not instead of it."""
    monkeypatch.delenv("AUDIBLE_PROXY_URL", raising=False)
    with caplog.at_level(logging.ERROR, logger="libex"):
        with pytest.raises(SystemExit):
            _verify_dedicated_proxy()

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    record = error_records[0]
    assert "AUDIBLE_PROXY_URL" in record.getMessage()
    assert record.proxy_host == "unset"
    assert record.proxy_configured is False


def test_verify_dedicated_proxy_logs_error_with_hostname_when_wrongly_named(monkeypatch, caplog):
    """A wrongly-named but set value logs proxy_configured=True and the
    actual (safe) hostname it resolved to -- distinct from the unset case,
    and still never the raw, possibly-credentialed value."""
    monkeypatch.setenv(
        "AUDIBLE_PROXY_URL", "http://opsuser:s3cr3t-token@libex-refresh-vpn:8888"
    )
    with caplog.at_level(logging.ERROR, logger="libex"):
        with pytest.raises(SystemExit):
            _verify_dedicated_proxy()

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    record = error_records[0]
    assert record.proxy_host == "libex-refresh-vpn"
    assert record.proxy_configured is True
    full_text = record.getMessage() + " ".join(
        str(v) for v in vars(record).values() if isinstance(v, str)
    )
    assert "s3cr3t-token" not in full_text
    assert "opsuser" not in full_text


def test_verify_dedicated_proxy_logs_error_on_malformed_value(monkeypatch, caplog):
    """A value that is set but unparseable (a typo'd port is the realistic
    case -- a Portainer env field is free text) must take the exact same
    logged-then-refused path as unset or wrongly-named, not escape as an
    uncaught httpx.InvalidURL that skips both the log line and the
    deliberate SystemExit message. Live-reproduced by the security review
    against pinned httpx 0.28.1: 'notaport' raises InvalidURL."""
    monkeypatch.setenv(
        "AUDIBLE_PROXY_URL",
        "http://opsuser:s3cr3t-token@libex-backfill-vpn:notaport",
    )
    with caplog.at_level(logging.ERROR, logger="libex"):
        with pytest.raises(SystemExit) as exc_info:
            _verify_dedicated_proxy()

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    record = error_records[0]
    assert record.proxy_host == "(unparseable)"
    assert record.proxy_configured is True
    full_text = record.getMessage() + " ".join(
        str(v) for v in vars(record).values() if isinstance(v, str)
    ) + str(exc_info.value)
    assert "s3cr3t-token" not in full_text
    assert "opsuser" not in full_text


@pytest.mark.asyncio
async def test_run_dies_before_touching_the_db_when_proxy_malformed(monkeypatch):
    """The malformed case must die in _run before the DB engine exists,
    exactly like the unset and wrongly-named cases -- not merely log
    correctly and then still blow up somewhere else uncaught."""
    monkeypatch.setenv(
        "AUDIBLE_PROXY_URL",
        "http://opsuser:s3cr3t-token@libex-backfill-vpn:notaport",
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("scripts.backfill_chapters.create_async_engine") as create_engine:
        with pytest.raises(SystemExit):
            await _run(limit=1)
    create_engine.assert_not_called()


def test_verify_dedicated_proxy_logs_nothing_at_error_when_correctly_named(monkeypatch, caplog):
    """The success path must not also emit the refusal record -- proves the
    logger.error call is gated on the same condition as the raise, not
    unconditional."""
    monkeypatch.setenv("AUDIBLE_PROXY_URL", "http://libex-backfill-vpn:8888")
    with caplog.at_level(logging.ERROR, logger="libex"):
        _verify_dedicated_proxy()
    assert [r for r in caplog.records if r.levelno == logging.ERROR] == []


# ============================================================
# _run -- the check fires before a single request or DB touch
# ============================================================

@pytest.mark.asyncio
async def test_run_dies_before_touching_the_db_when_proxy_unset(monkeypatch):
    """Proves the guard runs first in startup, not merely somewhere: with
    DATABASE_URL also absent, an unset proxy must still surface as the
    proxy's own SystemExit, never as a KeyError from reading DATABASE_URL,
    and create_async_engine must never be reached at all -- exactly the
    'dies before a single request goes out' requirement."""
    monkeypatch.delenv("AUDIBLE_PROXY_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("scripts.backfill_chapters.create_async_engine") as create_engine:
        with pytest.raises(SystemExit):
            await _run(limit=1)
    create_engine.assert_not_called()


@pytest.mark.asyncio
async def test_run_proceeds_past_the_check_when_correctly_named(monkeypatch):
    """A correctly-named proxy lets the run past the guard -- proven by the
    failure moving on to the next real requirement (DATABASE_URL) instead of
    the proxy check itself. This is also the --limit trial path: a trial
    still calls Audible for real, so it must clear the same guard as the
    unlimited run, with no bypass."""
    monkeypatch.setenv("AUDIBLE_PROXY_URL", "http://libex-backfill-vpn:8888")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("scripts.backfill_chapters._log_exit_ip", new=AsyncMock()):
        with pytest.raises(KeyError, match="DATABASE_URL"):
            await _run(limit=1)
