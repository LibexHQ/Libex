"""
Corpus refresh: dispatcher, ramp, and abort-condition tests.

refresh_corpus.py runs once, unattended, against the whole book corpus
(1,137,198 rows at the time this was written) with nobody watching it live --
a silent bug in how it groups work, paces itself, or decides to stop is not
caught by a human noticing something looks wrong, it is caught by the corpus
being wrong afterwards or the run never stopping. Scoped to what is pure and
therefore actually testable without a live Audible/Postgres pair: the region
grouping a bad chunk would 404 against, the gate a stuck limit would never
recover from, the ramp's own climb/retreat arithmetic, and the abort
conditions that are the only thing standing between a degraded exit IP and a
run that keeps hammering it. The dispatcher loop itself (_run/_dry_run), the
persist-queue drain, and the signal-handling wiring are operational glue
around this logic, not the content of it, and are left to a live run to
prove -- covering them here would mean re-implementing get_books_by_asins,
persist_queue and a testcontainers Postgres session as fakes, which is
exactly the "three nested mocks" shape that means the thing under test is
too coupled to unit test honestly.
"""

# Standard library
import asyncio

# Third party
import pytest

# Local
from scripts.refresh_corpus import (
    _Gate,
    _RegionSignal,
    _Ramp,
    _Run,
    _ThrottleSentinel,
    _check_abort,
    _chunks_for_page,
)
import scripts.refresh_corpus as refresh_corpus


# ============================================================
# _chunks_for_page — THE DISPATCHER
# ============================================================

def test_chunks_for_page_groups_by_region():
    """A chunk mixing regions would 404 most of what it asked for -- the
    grouping is not a convenience, it is what makes a chunk fetchable at all."""
    rows = [("B0US00001", "us"), ("B0UK00001", "uk"), ("B0US00002", "us")]

    chunks = _chunks_for_page(rows, size=50)

    assert {region for region, _ in chunks} == {"us", "uk"}
    for region, asins in chunks:
        assert all(a.startswith(f"B0{region.upper()}") for a in asins)


def test_chunks_for_page_splits_a_region_past_the_chunk_size():
    """One region's page is itself sliced at `size`, so a region with more
    rows on a page than the chunk size still produces fetchable chunks."""
    rows = [(f"B0US{i:05d}", "us") for i in range(5)]

    chunks = _chunks_for_page(rows, size=2)

    assert [len(asins) for _, asins in chunks] == [2, 2, 1]
    assert [a for _, asins in chunks for a in asins] == [r[0] for r in rows]


def test_chunks_for_page_collapses_duplicate_asins_within_a_region():
    """get_books_by_asins already dedupes internally -- doing it here too
    keeps the chunk count (and so the progress/rate accounting built on it)
    honest rather than counting the same ASIN twice."""
    rows = [("B0US00001", "us"), ("B0US00001", "us"), ("B0US00002", "us")]

    chunks = _chunks_for_page(rows, size=50)

    assert chunks == [("us", ["B0US00001", "B0US00002"])]


def test_chunks_for_page_orders_regions_deterministically():
    """Region order is sorted rather than page-arrival order, so two runs
    over the same page produce the same chunk sequence and log lines are
    comparable across restarts."""
    rows = [("B0UK00001", "uk"), ("B0AU00001", "au"), ("B0US00001", "us")]

    chunks = _chunks_for_page(rows, size=50)

    assert [region for region, _ in chunks] == ["au", "uk", "us"]


def test_chunks_for_page_empty_page_produces_no_chunks():
    """The exhausted-corpus case: an empty page must not produce an empty
    chunk, which the fan-out would then dispatch as a zero-ASIN fetch."""
    assert _chunks_for_page([], size=50) == []


# ============================================================
# _Gate — THE CONCURRENCY LIMIT THAT CAN SHRINK
# ============================================================

@pytest.mark.asyncio
async def test_gate_admits_up_to_the_limit():
    gate = _Gate(limit=2)
    await gate.acquire()
    await gate.acquire()
    assert gate.limit == 2


@pytest.mark.asyncio
async def test_gate_blocks_past_the_limit_until_a_release():
    """A third acquire past a limit of two must wait, not run -- this is the
    entire reason a Semaphore was replaced: the limit has to bind acquires
    made before a step-down too, not just ones made after it."""
    gate = _Gate(limit=1)
    await gate.acquire()

    third_ran = False

    async def _third():
        nonlocal third_ran
        await gate.acquire()
        third_ran = True

    task = asyncio.create_task(_third())
    await asyncio.sleep(0)
    assert third_ran is False

    await gate.release()
    await asyncio.sleep(0)
    assert third_ran is True
    task.cancel()


@pytest.mark.asyncio
async def test_gate_step_down_does_not_cancel_work_already_in_flight():
    """A step-down must take effect as tasks finish, not by revoking a permit
    already granted -- asyncio has no way to force a running task to stop,
    and set_limit must not pretend otherwise."""
    gate = _Gate(limit=2)
    await gate.acquire()
    await gate.acquire()

    await gate.set_limit(1)

    assert gate.limit == 1  # already-granted permits are unaffected


@pytest.mark.asyncio
async def test_gate_step_down_blocks_the_next_acquire_until_enough_release():
    """The new, lower limit binds the NEXT acquire: with two active against a
    limit stepped down to one, a third acquire must wait for two releases,
    not one."""
    gate = _Gate(limit=2)
    await gate.acquire()
    await gate.acquire()
    await gate.set_limit(1)

    admitted = False

    async def _waiter():
        nonlocal admitted
        await gate.acquire()
        admitted = True

    task = asyncio.create_task(_waiter())
    await asyncio.sleep(0)
    await gate.release()
    await asyncio.sleep(0)
    assert admitted is False  # one release still leaves 1 active >= limit 1

    await gate.release()
    await asyncio.sleep(0)
    assert admitted is True
    task.cancel()


# ============================================================
# _RegionSignal.p95 — THE LATENCY WINDOW
# ============================================================

def test_region_signal_p95_is_none_before_the_window_fills():
    """A region with too little traffic to fill its window must contribute to
    neither a step-up nor a step-down decision -- reporting a p95 on a
    half-full window would let one slow sample look like sustained
    degradation, or one lucky one look like sustained health."""
    signal = _RegionSignal()
    for _ in range(5):
        signal.latencies.append(0.1)

    assert len(signal.latencies) < signal.latencies.maxlen
    assert signal.p95() is None


def test_region_signal_p95_reports_the_95th_percentile_once_full():
    signal = _RegionSignal()
    window = signal.latencies.maxlen
    for i in range(window):
        signal.latencies.append(float(i))  # 0..window-1, evenly spaced

    p95 = signal.p95()

    ordered = list(range(window))
    expected = ordered[min(window - 1, int(window * 0.95))]
    assert p95 == expected


# ============================================================
# _Ramp — CLIMBS ON EVIDENCE, RETREATS ON ONE BAD REGION
# ============================================================

async def _fill_window(ramp, region, value, count=None):
    """Feeds `count` (default: a full window) clean samples of `value` for a
    region so its p95/best_p95 are established deterministically."""
    window = _RegionSignal().latencies.maxlen
    for _ in range(count or window):
        await ramp.record(region, value, failed=False)


@pytest.mark.asyncio
async def test_ramp_steps_up_only_once_every_warm_region_is_clean_together():
    """A step-up requires every region with a full window to be
    independently clean for RAMP_INTERVAL chunks AT THE SAME TIME. Filling
    the window itself already contributes LATENCY_WINDOW clean records to the
    streak (record() bumps clean_streak on every clean sample, not only once
    the window is full), so the boundary this pins is at RAMP_INTERVAL total
    clean records, not RAMP_INTERVAL on top of a full window."""
    gate = _Gate(limit=refresh_corpus.CONCURRENCY_START)
    ramp = _Ramp(gate)

    window = _RegionSignal().latencies.maxlen
    await _fill_window(ramp, "us", 0.1)
    await _fill_window(ramp, "uk", 0.1)
    assert ramp._regions["us"].clean_streak == window

    interval = refresh_corpus.RAMP_INTERVAL
    remaining_to_one_short = (interval - 1) - window
    for _ in range(remaining_to_one_short):
        await ramp.record("us", 0.1, failed=False)
        await ramp.record("uk", 0.1, failed=False)
    assert ramp._regions["us"].clean_streak == interval - 1
    assert gate.limit == refresh_corpus.CONCURRENCY_START  # not yet: one short

    await ramp.record("us", 0.1, failed=False)
    await ramp.record("uk", 0.1, failed=False)
    assert gate.limit > refresh_corpus.CONCURRENCY_START


@pytest.mark.asyncio
async def test_ramp_one_lagging_region_holds_back_a_warm_ones_climb():
    """The joint condition is the whole point: a region whose clean streak
    resets (it just reported one slower-than-usual sample, not enough to
    trip a step-down on its own) must hold the shared gate back even though
    another region has been clean for RAMP_INTERVAL chunks straight."""
    gate = _Gate(limit=refresh_corpus.CONCURRENCY_START)
    ramp = _Ramp(gate)
    window = _RegionSignal().latencies.maxlen
    interval = refresh_corpus.RAMP_INTERVAL

    await _fill_window(ramp, "us", 0.1)
    await _fill_window(ramp, "uk", 0.1)
    for _ in range(interval - window):
        await ramp.record("us", 0.1, failed=False)
    # "uk" lags a single step behind "us"
    for _ in range(interval - window - 1):
        await ramp.record("uk", 0.1, failed=False)

    assert ramp._regions["us"].clean_streak >= interval
    assert ramp._regions["uk"].clean_streak < interval
    assert gate.limit == refresh_corpus.CONCURRENCY_START  # held back by "uk"


@pytest.mark.asyncio
async def test_ramp_steps_down_on_one_degraded_region_and_freezes_the_climb():
    """One region past DEGRADE_P95_RATIO relative to its own best is enough
    to step the shared gate down and freeze further climbing for the rest of
    the run -- every region shares the same VPN exit, so one region's
    trouble is evidence about the shared ceiling, not just about itself."""
    gate = _Gate(limit=refresh_corpus.CONCURRENCY_START + refresh_corpus.RAMP_STEP)
    ramp = _Ramp(gate)
    # Past DEGRADE_WARMUP_SAMPLES before degrading, so this exercises a
    # settled baseline rather than first-window jitter -- the warmup
    # deliberately ignores the latter, which is a separate test.
    for _ in range(refresh_corpus.DEGRADE_WARMUP_SAMPLES):
        await ramp.record("us", 0.1, failed=False)
    starting_limit = gate.limit

    degraded = 0.1 * refresh_corpus.DEGRADE_P95_RATIO + 1.0
    await _fill_window(ramp, "us", degraded)

    assert gate.limit < starting_limit
    assert ramp.frozen is True


@pytest.mark.asyncio
async def test_ramp_a_failure_resets_the_clean_streak_without_touching_the_gate():
    """A single failed chunk must cost that region its clean streak (so a
    step-up needs a fresh run of successes) without itself moving the gate --
    only sustained latency degradation, not one failure, triggers a
    step-down."""
    gate = _Gate(limit=refresh_corpus.CONCURRENCY_START)
    ramp = _Ramp(gate)
    await _fill_window(ramp, "us", 0.1, count=5)

    await ramp.record("us", elapsed=None, failed=True)

    assert ramp._regions["us"].clean_streak == 0
    assert gate.limit == refresh_corpus.CONCURRENCY_START


# ============================================================
# _check_abort — THE CONDITIONS THAT STOP THE RUN
# ============================================================

def _sentinel_with_throttle(count):
    sentinel = _ThrottleSentinel()
    sentinel.throttled = count
    return sentinel


@pytest.mark.asyncio
async def test_check_abort_stops_on_any_throttled_response():
    """A single absorbed 429 is the earliest possible warning that the exit
    IP is in trouble -- this must not wait for a second one."""
    run = _Run(cursor=None)
    sentinel = _sentinel_with_throttle(1)

    _check_abort(run, sentinel)

    assert run.stopping is True
    assert run.abort_reason is not None and "throttled" in run.abort_reason


@pytest.mark.asyncio
async def test_check_abort_does_not_stop_with_no_throttle_and_a_clean_history():
    run = _Run(cursor=None)
    sentinel = _ThrottleSentinel()

    _check_abort(run, sentinel)

    assert run.stopping is False
    assert run.abort_reason is None


def test_check_abort_stops_on_chunk_failure_rate_at_the_threshold():
    """The rate check only engages once ABORT_CHUNK_FAILURE_MIN samples exist
    -- fewer than that and a run of early bad luck could trip it on noise
    rather than a real trend."""
    run = _Run(cursor=None)
    sentinel = _ThrottleSentinel()
    minimum = refresh_corpus.ABORT_CHUNK_FAILURE_MIN
    rate = refresh_corpus.ABORT_CHUNK_FAILURE_RATE
    failing = int(minimum * rate)
    for _ in range(failing):
        run.recent_failures.append(True)
    for _ in range(minimum - failing):
        run.recent_failures.append(False)

    _check_abort(run, sentinel)

    assert run.stopping is True
    assert "failure rate" in run.abort_reason


def test_check_abort_does_not_stop_below_the_minimum_sample_count():
    """Failing every one of a handful of chunks must not abort the run before
    ABORT_CHUNK_FAILURE_MIN samples exist to judge a rate from."""
    run = _Run(cursor=None)
    sentinel = _ThrottleSentinel()
    minimum = refresh_corpus.ABORT_CHUNK_FAILURE_MIN
    for _ in range(minimum - 1):
        run.recent_failures.append(True)

    _check_abort(run, sentinel)

    assert run.stopping is False


def test_check_abort_reports_only_the_first_reason():
    """abort() is called once per condition-check cycle at most in practice,
    but the guard itself must still keep the first reason if it is ever
    called twice -- the exit line should name the actual trigger, not
    whatever happened to run last."""
    run = _Run(cursor=None)
    run.abort("first reason")

    run.abort("second reason")

    assert run.abort_reason == "first reason"


@pytest.mark.asyncio
async def test_the_ramp_does_not_freeze_on_first_window_jitter():
    """A degrade signal from a region that has barely been sampled must not
    freeze the ramp for the rest of the run.

    Measured on the first live pass: the ramp froze at the opening rung 1.3
    minutes in against a perfectly healthy exit. best_p95 takes any new
    minimum, so the first full window sets the bar, and the samples that
    tripped it were 1104ms against a 469ms best -- 2.35x, past the ratio, on
    ordinary jitter. The freeze is permanent, so that run spent its whole life
    at CONCURRENCY_START for no reason.

    Feeds a fast baseline and then a spike, both inside the warmup, and
    asserts the ramp is still live."""
    gate = _Gate(limit=refresh_corpus.CONCURRENCY_START)
    ramp = _Ramp(gate)

    window = refresh_corpus.LATENCY_WINDOW
    for _ in range(window):
        await ramp.record("us", 0.469, failed=False)
    # A spike well past the ratio, still inside the warmup.
    for _ in range(window):
        await ramp.record("us", 1.104, failed=False)

    assert ramp.frozen is False, "froze on jitter before it had a settled baseline"
    assert gate.limit == refresh_corpus.CONCURRENCY_START
