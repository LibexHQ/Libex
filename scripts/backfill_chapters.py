"""
One-off chapter backfill.

Walks books with chapters_checked_at IS NULL, fetches each book's chapters
from Audible, and stores them slowly so the traffic doesn't look like a
scraper. Concurrency ramps 3 -> 10 on clean latency evidence, drops to 1 on
the first 429/401/403 and aborts on a second. A 404, or a confirmed-permanent
non-404 status (currently just 400), marks the book checked without
retrying; anything else is retried. Reuses the app's own fetch/normalize/
write path, so stored data matches an on-demand fetch.

RUN IT (its own container, its own dedicated VPN exit -- AUDIBLE_PROXY_URL
must name it explicitly; the run refuses to start unless its hostname
contains "backfill", see _verify_dedicated_proxy):

    docker run -d --name libex-chapter-backfill \\
      --network libex-proxy \\
      --network libex_default \\
      -e AUDIBLE_PROXY_URL=http://libex-backfill-vpn:8888 \\
      -e DATABASE_URL=<same as the app> \\
      ghcr.io/libexhq/libex:latest \\
      python -m scripts.backfill_chapters --limit 5     # dry run; drop --limit for the real run

Both networks are needed: libex-proxy reaches the VPN sidecar, and the app
stack's own network is the only place the `postgres` host in DATABASE_URL
resolves. Its real name is the stack's, not necessarily libex_default --
`docker inspect libex --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'`
prints both; whichever is not libex-proxy is the stack's. Repeating
--network on `docker run` needs Engine 25.0+; on older ones use docker
create, docker network connect, then docker start.

Stop with `docker stop libex-chapter-backfill` -- it finishes in-flight
books, commits them, and exits cleanly (exit 0; exit 1 means the ratchet or
the NONE-rate guard aborted the run).

ENVIRONMENT.

    DATABASE_URL               required. Same database the app uses.
    AUDIBLE_PROXY_URL          required. Hostname must contain "backfill".
    LOG_LEVEL                  INFO    DEBUG, INFO, WARNING or ERROR. WARNING+
                                       drops the RESUME CURSOR line and the
                                       429/5xx signals the ratchet depends on.
    AXIOM_TOKEN                (unset) set to also ship logs to Axiom.
    AXIOM_DATASET              libex
    LOG_RETENTION_DAYS         7       0 keeps everything.
    BACKFILL_DELAY_MIN         0.7     per-request delay floor (s), once
                                       ratcheted down to concurrency 1.
    BACKFILL_DELAY_MAX         2.0     per-request delay ceiling (s) at the floor.
    BACKFILL_ACTIVE_HOURS      12.0    hours awake before a pause.
    BACKFILL_PAUSE_MIN_HOURS   4.0     shortest pause between active stretches.
    BACKFILL_PAUSE_MAX_HOURS   11.0    longest pause between active stretches.
    BACKFILL_CHUNK_SIZE        500     keyset page size for the corpus walk.
    BACKFILL_ERROR_WINDOW_SECONDS   65.0   rolling window for the soft back-off signal.
    BACKFILL_ERROR_THRESHOLD_RATE   0.5    signal share in that window that trips cooldown.
    BACKFILL_ERROR_MIN_SAMPLES      10     samples required before that rate can trip.
    BACKFILL_ERROR_COOLDOWN         1800.0 cooldown length (s); also the ratchet's own.
    BACKFILL_PROGRESS_EVERY         50     books between progress lines.
"""

# Standard library
import argparse
import asyncio
import logging
import os
import random
import signal
import time
from collections import deque
from datetime import datetime, timezone

# Third party
import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Database
from app.db.models import Book, Track

# Core
from app.core.exceptions import AudibleAPIException, NotFoundException
from app.core.logging import get_logger, setup_logging

# Services
from app.services.audible import client as audible_client
from app.services.audible.client import audible_get
from app.services.audible.books import _normalize_chapters


logger = get_logger()


# --- tunables (env-overridable so behaviour can be adjusted without a rebuild) ---

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# per-request delay once the ratchet has dropped the run to its floor (see
# _Ratchet): a random draw in [MIN, MAX] seconds.
DELAY_MIN = _env_float("BACKFILL_DELAY_MIN", 0.7)
DELAY_MAX = _env_float("BACKFILL_DELAY_MAX", 2.0)

# per-slot jitter while running above the floor -- desynchronisation, not
# rate control. Not env-tunable; nobody has needed to turn this knob yet.
SLOT_JITTER_MIN = 0.25
SLOT_JITTER_MAX = 0.75

# macro on/off: run for ACTIVE_HOURS, then pause a random number of hours in
# [PAUSE_MIN, PAUSE_MAX], then resume. Breaks up the daily traffic signature.
ACTIVE_HOURS = _env_float("BACKFILL_ACTIVE_HOURS", 12.0)
PAUSE_MIN_HOURS = _env_float("BACKFILL_PAUSE_MIN_HOURS", 4.0)
PAUSE_MAX_HOURS = _env_float("BACKFILL_PAUSE_MAX_HOURS", 11.0)

# --- concurrency ramp --------------------------------------------------------
#
# Plain module constants, not env-tunable. Ceiling fixed at 10, never wider
# than AUDIBLE_CONCURRENCY_LIMIT (see the assert below).
CONCURRENCY_FLOOR = 1
CONCURRENCY_START = 3
CONCURRENCY_STEP = 3
CONCURRENCY_CEILING = 10

# Never wider than the API's own fan-out width -- asserted, not just
# commented, so this can't silently regress if either constant moves.
assert CONCURRENCY_CEILING <= audible_client.AUDIBLE_CONCURRENCY_LIMIT, (
    f"CONCURRENCY_CEILING ({CONCURRENCY_CEILING}) must not exceed "
    f"AUDIBLE_CONCURRENCY_LIMIT ({audible_client.AUDIBLE_CONCURRENCY_LIMIT}) -- "
    "this run's ramp must never widen past the API's own fan-out limit."
)

# Per-rung dwell: BOTH floors must be met before the next step up.
RAMP_MIN_SECONDS = 60.0
RAMP_MIN_REQUESTS = 300

# Degradation, ported from refresh_corpus: latency sampled per Audible
# request, tracked per region, a level's p95 compared only against that
# region's own best. Past this ratio, the run steps down one rung and
# freezes further climbing for the rest of the run. Three warmup windows
# before the check may act, so a cold exit's first-window jitter can't trip it.
LATENCY_WINDOW = 60
DEGRADE_P95_RATIO = 2.0
DEGRADE_WARMUP_SAMPLES = LATENCY_WINDOW * 3

# General back-off: a timeout/connection failure or a 5xx (see
# _is_backoff_signal). Time-based rather than count-based so the window
# spans the same real time at any concurrency. 429/401/403 are excluded --
# see _Ratchet for the stricter mechanism those get instead.
ERROR_WINDOW_SECONDS = _env_float("BACKFILL_ERROR_WINDOW_SECONDS", 65.0)
ERROR_THRESHOLD_RATE = _env_float("BACKFILL_ERROR_THRESHOLD_RATE", 0.5)
ERROR_MIN_SAMPLES = _env_int("BACKFILL_ERROR_MIN_SAMPLES", 10)
ERROR_COOLDOWN = _env_float("BACKFILL_ERROR_COOLDOWN", 1800.0)  # 30 min -- also the ratchet's own cooldown

# Sustained 5xx: a handful in a long run is unremarkable, a sustained run
# means real trouble.
ABORT_5XX_WITHIN = 20
ABORT_5XX_WINDOW_SECONDS = 120.0

# Rolling NONE-rate abort -- see _NoneRateGuard. BASELINE_MIN_SAMPLES is the
# settled history required before a comparison is trusted; ABSOLUTE_FLOOR
# keeps a genuinely NONE-heavy catalog segment from tripping this alone;
# SPIKE_MULTIPLIER is how far past baseline counts as a spike.
NONE_RATE_WINDOW = 60
NONE_RATE_BASELINE_MIN_SAMPLES = 200
NONE_RATE_ABSOLUTE_FLOOR = 0.5
NONE_RATE_SPIKE_MULTIPLIER = 3.0

# keyset page size for the corpus walk (see _read_page) -- not a rate knob.
CHUNK_SIZE = _env_int("BACKFILL_CHUNK_SIZE", 500)

# how often to log a progress line
PROGRESS_EVERY = _env_int("BACKFILL_PROGRESS_EVERY", 50)

CHAPTERS_PATH = "/1.0/content/{asin}/metadata"
CHAPTERS_PARAMS = {
    "response_groups": "chapter_info, always-returned, content_reference, content_url",
    "quality": "High",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- graceful stop ---------------------------------------------------------

class _Stopper:
    """Flips to stopping on SIGTERM/SIGINT so the loop can exit between books."""

    def __init__(self) -> None:
        self.stopping = False

    def request(self, *_args) -> None:
        if not self.stopping:
            logger.info("Backfill: stop requested, will exit after the in-flight books finish")
        self.stopping = True


# --- proxy containment (unchanged; see LIBEX_LESSONS_HARD_WON.md) ----------

def _proxy_host_for_log(proxy: str | None) -> str:
    """
    Best-effort hostname for a log line -- never the raw AUDIBLE_PROXY_URL,
    since httpx's proxy= accepts embedded credentials
    (http://user:pass@host:port) and Settings stores this as a plain str.
    Never raises -- a malformed value becomes "(unparseable)".
    """
    if not proxy:
        return "direct"
    try:
        host = httpx.URL(proxy).host
    except Exception:
        return "(unparseable)"
    return host or "(unparseable)"


def _verify_dedicated_proxy() -> None:
    """
    Refuses to start unless AUDIBLE_PROXY_URL is set and its hostname
    contains "backfill" -- otherwise this script's Audible traffic would
    egress from the container's own address, the same one the live service
    answers on. Checked against the hostname only, since the real proxy
    value may carry embedded credentials and must never reach a log line or
    exception message. Logged before the SystemExit, since SystemExit alone
    never reaches the log handlers.
    """
    proxy = os.environ.get("AUDIBLE_PROXY_URL", "")
    host = _proxy_host_for_log(proxy) if proxy else ""
    if not proxy or "backfill" not in host:
        detail = f"host {host!r}" if proxy else "unset"
        logger.error(
            "Backfill: refusing to start, AUDIBLE_PROXY_URL does not name "
            "a backfill-dedicated exit",
            extra={"proxy_host": host or "unset", "proxy_configured": bool(proxy)},
        )
        raise SystemExit(
            f"AUDIBLE_PROXY_URL ({detail}) does not name a "
            f"backfill-dedicated exit. Refusing to start against what may "
            f"be the shared production proxy or the container's own direct "
            "egress -- point this at the dedicated backfill exit before "
            "starting."
        )


async def _log_exit_ip() -> None:
    """One-time startup check: confirm which IP the configured proxy actually exits from.

    Also called again by the ratchet's first trip (see _Ratchet and its call
    site in _run) -- a floor-and-freeze is exactly the moment worth
    reconfirming which IP just got throttled or blocked."""
    proxy = os.environ.get("AUDIBLE_PROXY_URL") or None
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=15.0) as client:
            resp = await client.get("https://api.ipify.org")
            logger.info(
                f"Backfill: exit IP {resp.text.strip()} "
                f"(via {_proxy_host_for_log(proxy)})"
            )
    except Exception as e:
        logger.warning(f"Backfill: could not determine exit IP: {type(e).__name__}: {e}")


# --- concurrency gate --------------------------------------------------------

class _Gate:
    """
    A concurrency gate whose limit can move while tasks wait on it.

    asyncio.Semaphore can't shrink -- releasing extra permits grows it, but
    nothing takes permits back. A condition variable over an active count
    can, and a step down takes effect as tasks finish rather than by
    cancelling work already in flight.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0
        self._cond = asyncio.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    async def set_limit(self, limit: int) -> None:
        async with self._cond:
            self._limit = limit
            self._cond.notify_all()

    async def acquire(self) -> None:
        async with self._cond:
            while self._active >= self._limit:
                await self._cond.wait()
            self._active += 1

    async def release(self) -> None:
        async with self._cond:
            self._active -= 1
            self._cond.notify()


# --- latency-based ramp, ported from refresh_corpus with a dwell floor -----

class _RegionSignal:
    """One region's latency window, best p95, and clean-streak dwell state."""

    def __init__(self) -> None:
        self.latencies: deque[float] = deque(maxlen=LATENCY_WINDOW)
        self.clean_streak = 0
        self.streak_started: float | None = None
        self.best_p95: float | None = None
        self.samples = 0

    def p95(self) -> float | None:
        if len(self.latencies) < LATENCY_WINDOW:
            return None
        ordered = sorted(self.latencies)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]

    def dwell_met(self) -> bool:
        """Both RAMP_MIN_REQUESTS clean requests AND RAMP_MIN_SECONDS of
        wall-clock time since the current clean streak began."""
        return (
            self.clean_streak >= RAMP_MIN_REQUESTS
            and self.streak_started is not None
            and time.monotonic() - self.streak_started >= RAMP_MIN_SECONDS
        )


class _Ramp:
    """
    Climbs the shared gate on evidence from every region and steps it down
    on degradation in any one. Step-up requires dwell_met() (time AND
    count) rather than a bare clean-streak count.
    """

    def __init__(self, gate: _Gate) -> None:
        self._gate = gate
        self._regions: dict[str, _RegionSignal] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def freeze(self) -> None:
        """Called by the ratchet when it drops to the floor -- makes this
        ramp's own degrade/step-up machinery permanently inert."""
        self._frozen = True

    def _signal(self, region: str) -> _RegionSignal:
        return self._regions.setdefault(region, _RegionSignal())

    async def record(self, region: str, elapsed: float | None, failed: bool) -> None:
        signal = self._signal(region)

        if failed:
            signal.clean_streak = 0
            signal.streak_started = None
            return

        if signal.clean_streak == 0:
            signal.streak_started = time.monotonic()
        signal.clean_streak += 1
        if elapsed is not None:
            signal.latencies.append(elapsed)
            signal.samples += 1

        p95 = signal.p95()
        if p95 is None:
            return

        if signal.best_p95 is None or p95 < signal.best_p95:
            signal.best_p95 = p95

        warming_up = signal.samples < DEGRADE_WARMUP_SAMPLES

        if not warming_up and not self._frozen and p95 > signal.best_p95 * DEGRADE_P95_RATIO:
            await self._step_down(region, signal, p95)
            return

        if not self._frozen and signal.dwell_met():
            warm = [s for s in self._regions.values() if s.best_p95 is not None]
            if warm and all(s.dwell_met() for s in warm):
                await self._step_up()

    async def _step_up(self) -> None:
        if self._gate.limit >= CONCURRENCY_CEILING:
            for signal in self._regions.values():
                signal.clean_streak = 0
                signal.streak_started = None
            return
        new_limit = min(CONCURRENCY_CEILING, self._gate.limit + CONCURRENCY_STEP)
        # Reset before the await so a concurrent record() that runs while
        # this is suspended can't independently re-satisfy "all regions
        # clean" and call _step_up() again before this climb has landed.
        for signal in self._regions.values():
            signal.clean_streak = 0
            signal.streak_started = None
            signal.latencies.clear()
        await self._gate.set_limit(new_limit)
        logger.info("Backfill: ramping up", extra={
            "concurrency": new_limit,
            "ceiling": CONCURRENCY_CEILING,
            "regions_warm": len(self._regions),
        })

    async def _step_down(self, region: str, signal: _RegionSignal, p95: float) -> None:
        self._frozen = True
        new_limit = max(CONCURRENCY_START, self._gate.limit - CONCURRENCY_STEP)
        await self._gate.set_limit(new_limit)
        best_p95 = signal.best_p95
        for s in self._regions.values():
            s.clean_streak = 0
            s.streak_started = None
            s.latencies.clear()
        logger.warning("Backfill: latency degraded, holding below the ceiling", extra={
            "region": region,
            "concurrency": new_limit,
            "p95_ms": round(p95 * 1000, 1),
            "best_p95_ms": round((best_p95 or 0) * 1000, 1),
            "ratio": DEGRADE_P95_RATIO,
        })


# --- throttle sentinel, ported from refresh_corpus --------------------------

class _ThrottleSentinel(logging.Handler):
    """
    Watches for the throttle line audible_get emits on every 429/5xx,
    including ones a retry absorbed -- otherwise invisible. Keys on the
    structured fields (status_code with attempts_left), emitted by exactly
    one call site, rather than message text.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.throttled = 0
        self.server_errors: deque[float] = deque()

    def emit(self, record: logging.LogRecord) -> None:
        status = getattr(record, "status_code", None)
        if status is None or not hasattr(record, "attempts_left"):
            return
        if status == 429:
            self.throttled += 1
        elif isinstance(status, int) and 500 <= status < 600:
            now = time.monotonic()
            self.server_errors.append(now)
            while self.server_errors and now - self.server_errors[0] > ABORT_5XX_WINDOW_SECONDS:
                self.server_errors.popleft()

    @property
    def sustained_server_errors(self) -> bool:
        now = time.monotonic()
        while self.server_errors and now - self.server_errors[0] > ABORT_5XX_WINDOW_SECONDS:
            self.server_errors.popleft()
        return len(self.server_errors) >= ABORT_5XX_WITHIN


# --- the ratchet: first 429/401/403 to the floor, second aborts ------------

class _Ratchet:
    """
    First 429 (raised or sentinel-absorbed) or 401/403, ever: drop to
    CONCURRENCY_FLOOR, freeze the ramp, restore the historical delay, cool
    down, re-log the exit IP. Any further such signal after that: abort.

    observe() takes the book's own auth_trouble flag (401/403 only) plus the
    sentinel's running total, folding in the delta of new 429s since the
    last call.
    """

    def __init__(self) -> None:
        self.tripped = False
        self.abort_reason: str | None = None
        self._last_sentinel_throttled = 0

    def observe(self, auth_trouble: bool, sentinel: _ThrottleSentinel) -> str | None:
        new_events = 1 if auth_trouble else 0
        new_events += max(0, sentinel.throttled - self._last_sentinel_throttled)
        self._last_sentinel_throttled = sentinel.throttled

        result: str | None = None
        for _ in range(new_events):
            if self.tripped:
                if self.abort_reason is None:
                    self.abort_reason = (
                        "a second 429/401/403 arrived after the ratchet had "
                        "already dropped the run to its floor"
                    )
                result = "abort"
            else:
                self.tripped = True
                result = "floor"
        return result


# --- time-based general back-off window -------------------------------------

class _BackoffWindow:
    """
    Rolling time-based window of recent soft-backoff signals (a timeout,
    connection failure, or 5xx -- see _is_backoff_signal).
    """

    def __init__(self) -> None:
        self._events: deque[tuple[float, bool]] = deque()

    def record(self, is_signal: bool) -> None:
        now = time.monotonic()
        self._events.append((now, is_signal))
        while self._events and now - self._events[0][0] > ERROR_WINDOW_SECONDS:
            self._events.popleft()

    @property
    def should_cooldown(self) -> bool:
        if len(self._events) < ERROR_MIN_SAMPLES:
            return False
        signals = sum(1 for _, s in self._events if s)
        return signals / len(self._events) >= ERROR_THRESHOLD_RATE

    def clear(self) -> None:
        self._events.clear()


# --- rolling NONE-rate abort -------------------------------------------------

class _NoneRateGuard:
    """
    Aborts the run when the recent share of NONE outcomes spikes well past
    this run's own established baseline.

    The baseline is computed from history strictly before the recent
    window, so a spike inside the window can't drag its own baseline up.
    """

    def __init__(self) -> None:
        self.window: deque[bool] = deque(maxlen=NONE_RATE_WINDOW)
        self.total = 0
        self.total_none = 0

    def record(self, is_none: bool) -> bool:
        """Returns whether this call detected a spike worth aborting on."""
        self.window.append(is_none)
        self.total += 1
        if is_none:
            self.total_none += 1

        if len(self.window) < NONE_RATE_WINDOW:
            return False

        window_none = sum(self.window)
        prior_total = self.total - len(self.window)
        prior_none = self.total_none - window_none
        if prior_total < NONE_RATE_BASELINE_MIN_SAMPLES:
            return False

        baseline = prior_none / prior_total
        recent = window_none / len(self.window)
        return recent >= NONE_RATE_ABSOLUTE_FLOOR and recent >= baseline * NONE_RATE_SPIKE_MULTIPLIER


# --- keyset paging with a one-shot wrap-around ------------------------------

async def _read_page(
    session: AsyncSession, cursor: str | None, size: int
) -> list[tuple[str, str, datetime | None]]:
    """
    One keyset page of (asin, region, chapters_checked_at), ordered by the
    primary key.

    Filtering to chapters_checked_at IS NULL happens after this call, in
    Python, not in the WHERE clause -- that column has no index, so a
    filtered query would heap-fetch every row of the stamped prefix on
    every page.
    """
    stmt = select(Book.asin, Book.region, Book.chapters_checked_at).order_by(Book.asin).limit(size)
    if cursor is not None:
        stmt = stmt.where(Book.asin > cursor)
    result = await session.execute(stmt)
    return [(row[0], row[1], row[2]) for row in result.all()]


def _advance_cursor(
    rows: list[tuple[str, str, datetime | None]], cursor: str | None, wrapped: bool
) -> tuple[str | None, bool, bool]:
    """
    Pure wrap-around policy: reached the end of the corpus once -> wrap the
    cursor back to the start for one more pass; reached it a second time,
    already wrapped -> genuinely done.

    Returns (next_cursor, next_wrapped, done).
    """
    if not rows:
        if wrapped:
            return cursor, wrapped, True
        return None, True, False
    return rows[-1][0], wrapped, False


async def _mark_checked(session: AsyncSession, asin: str) -> None:
    """Stamps chapters_checked_at so this book leaves the queue for good."""
    await session.execute(
        update(Book).where(Book.asin == asin).values(chapters_checked_at=_now())
    )
    await session.commit()


async def _store_chapters(session: AsyncSession, asin: str, chapters: dict) -> None:
    """Writes the track row. Upserts by asin so a re-run just refreshes it."""
    from sqlalchemy.dialects.postgresql import insert

    stmt = insert(Track).values(
        asin=asin,
        chapters=chapters,
        created_at=_now(),
        updated_at=_now(),
    ).on_conflict_do_update(
        index_elements=["asin"],
        set_={"chapters": chapters, "updated_at": _now()},
    )
    await session.execute(stmt)
    await session.commit()


class _Outcome:
    STORED = "stored"        # chapters fetched and saved
    NONE = "none"            # resolved but Audible exposes no chapters
    NOT_FOUND = "not_found"  # 404 -- record has no fetchable chapters anywhere (terminal)
    PERMANENT = "permanent"  # confirmed-permanent non-404 status (currently just 400) -- terminal
    ERROR = "error"          # unresolved failure; retried, and counts toward the plain error total


# Non-404 upstream statuses treated as a permanent fact about the specific
# record rather than a failure worth retrying. Limited to 400 -- the only
# one actually observed in production. 401/403 are deliberately NOT here --
# see _is_backoff_signal and _Ratchet, which keep those retryable and
# promote them to the ratchet instead.
_PERMANENT_UPSTREAM_STATUSES = {400}


def _is_backoff_signal(upstream_status: int | None) -> bool:
    """True only for a timeout/connection failure (upstream_status is None)
    or a 5xx. 401/403 and 429 are excluded -- both are promoted to
    _Ratchet instead. Confirmed per-record facts (404, and any status in
    _PERMANENT_UPSTREAM_STATUSES) never reach this function at all."""
    if upstream_status is None:
        return True
    return 500 <= upstream_status < 600


async def _process_one(
    session: AsyncSession, asin: str, region: str
) -> tuple[str, bool, bool, float]:
    """
    Fetches and stores one book's chapters.

    Outcomes: STORED/NONE/NOT_FOUND/PERMANENT mark the book checked and
    leave the queue; none of these count as an error. ERROR is left
    unmarked so a later pass retries it.

    Returns (outcome, is_backoff_signal, is_ratchet_signal, elapsed).
    is_ratchet_signal is only ever True for a raised 401/403 -- a 429 never
    sets it here, since audible_get's own retry branch already logged it for
    _ThrottleSentinel to see.
    """
    started = time.monotonic()
    try:
        data = await audible_get(region, CHAPTERS_PATH.format(asin=asin), CHAPTERS_PARAMS)
    except NotFoundException:
        elapsed = time.monotonic() - started
        # 404 -- terminal. This record has no chapter metadata (confirmed across
        # all regions for the ISBN-keyed class). Mark it and move on; do not retry
        # and do not treat as an error.
        await _mark_checked(session, asin)
        return _Outcome.NOT_FOUND, False, False, elapsed
    except AudibleAPIException as e:
        elapsed = time.monotonic() - started
        if e.upstream_status in _PERMANENT_UPSTREAM_STATUSES:
            # Confirmed permanent per-ASIN fact -- see _PERMANENT_UPSTREAM_STATUSES.
            # Mark it and move on exactly like a 404: no retry, not an error.
            await _mark_checked(session, asin)
            return _Outcome.PERMANENT, False, False, elapsed
        logger.warning(f"Backfill: fetch error for {asin} ({region}): {type(e).__name__}: {e}")
        is_auth_trouble = e.upstream_status in (401, 403)
        return _Outcome.ERROR, _is_backoff_signal(e.upstream_status), is_auth_trouble, elapsed
    except Exception as e:
        elapsed = time.monotonic() - started
        # Never became an AudibleAPIException, so there is no upstream_status
        # to classify (audible_get returned 200 and the body failed to parse,
        # a bug, etc.) -- always retried, but never a signal for either the
        # back-off window or the ratchet, since pausing fixes nothing about it.
        logger.warning(f"Backfill: fetch error for {asin} ({region}): {type(e).__name__}: {e}")
        return _Outcome.ERROR, False, False, elapsed

    elapsed = time.monotonic() - started

    # Resolved, but no chapters present.
    if not data.get("content_metadata", {}).get("chapter_info"):
        await _mark_checked(session, asin)
        return _Outcome.NONE, False, False, elapsed

    try:
        chapters = _normalize_chapters(data, asin)
        await _store_chapters(session, asin, chapters)
        await _mark_checked(session, asin)
        return _Outcome.STORED, False, False, elapsed
    except Exception as e:
        # write failure -- leave unmarked so a later pass retries it. Not a
        # signal either: a local write failure says nothing about the exit IP.
        logger.warning(f"Backfill: store failed for {asin}: {type(e).__name__}: {e}")
        await session.rollback()
        return _Outcome.ERROR, False, False, elapsed


# --- run state ---------------------------------------------------------------

class _Run:
    """Counters, mutable pacing state, and the abort flag every concurrent
    unit and the dispatch loop share."""

    def __init__(self) -> None:
        self.processed = 0
        self.dispatched = 0
        self.stored = 0
        self.none = 0
        self.not_found = 0
        self.permanent = 0
        self.errors = 0
        self.abort_reason: str | None = None
        self.floor_pending = False
        self.soft_pause_pending = False
        self.delay_min = SLOT_JITTER_MIN
        self.delay_max = SLOT_JITTER_MAX
        self.ratchet = _Ratchet()
        self.backoff_window = _BackoffWindow()
        self.none_guard = _NoneRateGuard()

    @property
    def stopping(self) -> bool:
        return self.abort_reason is not None

    def abort(self, reason: str) -> None:
        if self.abort_reason is None:
            self.abort_reason = reason


async def _interruptible_sleep(seconds: float, stopper: _Stopper, run: "_Run | None" = None) -> None:
    """
    Sleeps in short slices so a stop request is noticed promptly.

    run is optional (some call sites run before a _Run exists) but should
    be passed whenever one is in scope, so a long sleep also wakes on
    run.abort_reason being set by another in-flight unit.
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end and not stopper.stopping and not (run is not None and run.stopping):
        await asyncio.sleep(min(1.0, end - time.monotonic()))


def _log_progress(run: _Run, gate: _Gate) -> None:
    logger.info(
        "Backfill: progress",
        extra={
            "processed": run.processed,
            "stored": run.stored,
            "no_chapters": run.none,
            "not_found": run.not_found,
            "permanent": run.permanent,
            "errors": run.errors,
            "concurrency": gate.limit,
            "ratchet_tripped": run.ratchet.tripped,
        },
    )


async def _dispatch_one(
    asin: str,
    region: str,
    Session: async_sessionmaker,
    run: _Run,
    gate: _Gate,
    ramp: _Ramp,
    sentinel: _ThrottleSentinel,
    stopper: _Stopper,
) -> None:
    """
    One concurrent unit of work. Releases the gate permit, then sleeps this
    slot's own jittered delay AFTER releasing, so the delay never occupies a
    concurrency slot doing nothing. Every unit gets its own AsyncSession,
    since AsyncSession is not concurrency-safe.

    Every escape from the block below becomes a counted ERROR outcome
    rather than an exception that keeps propagating -- _run collects these
    tasks with return_exceptions=True, so an uncaught exception here would
    otherwise vanish silently with the cursor still advancing.
    """
    outcome, is_backoff_signal, is_ratchet_signal, elapsed = _Outcome.ERROR, False, False, None
    try:
        try:
            async with Session() as session:
                outcome, is_backoff_signal, is_ratchet_signal, elapsed = await _process_one(
                    session, asin, region
                )
        except Exception as exc:
            # Reachable in practice: _mark_checked runs unguarded on the
            # NOT_FOUND/PERMANENT/NONE paths, and this slice's own
            # statement_timeout can raise on exactly those calls. Feeds the
            # ramp and the general back-off window, but not the ratchet --
            # a DB-side failure says nothing about the exit IP.
            logger.warning("Backfill: unit failed", extra={
                "asin": asin,
                "region": region,
                "error_type": type(exc).__name__,
                "sqlstate": getattr(getattr(exc, "orig", None), "sqlstate", None),
            })
            outcome, is_backoff_signal, is_ratchet_signal, elapsed = _Outcome.ERROR, True, False, None
    finally:
        await gate.release()

    # This bookkeeping is atomic per completed task under cooperative
    # scheduling except across two real suspension points: `await
    # ramp.record(...)` on a ramp transition, and `await gate.set_limit(...)`
    # in the ratchet's floor branch. The only place that matters: the
    # progress-line check below can be skipped (never duplicated) if another
    # task's own check lands in that window.
    run.processed += 1
    if outcome == _Outcome.STORED:
        run.stored += 1
    elif outcome == _Outcome.NONE:
        run.none += 1
    elif outcome == _Outcome.NOT_FOUND:
        run.not_found += 1
    elif outcome == _Outcome.PERMANENT:
        run.permanent += 1
    elif outcome == _Outcome.ERROR:
        run.errors += 1

    ramp_failed = outcome == _Outcome.ERROR
    await ramp.record(region, None if ramp_failed else elapsed, ramp_failed)

    run.backoff_window.record(is_backoff_signal)
    if run.backoff_window.should_cooldown:
        run.soft_pause_pending = True

    ratchet_result = run.ratchet.observe(is_ratchet_signal, sentinel)
    if ratchet_result == "floor":
        run.delay_min, run.delay_max = DELAY_MIN, DELAY_MAX
        await gate.set_limit(CONCURRENCY_FLOOR)
        ramp.freeze()
        run.floor_pending = True
    elif ratchet_result == "abort":
        run.abort(run.ratchet.abort_reason)

    if sentinel.sustained_server_errors:
        run.abort(
            f"{len(sentinel.server_errors)} upstream 5xx within "
            f"{ABORT_5XX_WINDOW_SECONDS:.0f}s"
        )

    if run.none_guard.record(outcome == _Outcome.NONE):
        run.abort(
            "the recent share of NONE outcomes spiked well past this run's "
            "own established baseline -- see _NoneRateGuard"
        )

    if run.processed % PROGRESS_EVERY == 0:
        _log_progress(run, gate)

    await _interruptible_sleep(random.uniform(run.delay_min, run.delay_max), stopper, run)


async def _handle_pending_pauses(
    run: _Run, inflight: set[asyncio.Task], stopper: _Stopper
) -> None:
    """
    Takes the ratchet's or the soft window's enforced pause, if either
    tripped since the last check -- draining whatever's in flight first.

    Call this from every point in the dispatch loop where a just-completed
    task could have set either flag, not only inside the per-task loop
    body: a flag set by the last task dispatched on a page has no further
    iteration to be noticed from.
    """
    if run.floor_pending:
        run.floor_pending = False
        logger.error(
            "Backfill: ratchet tripped -- dropping to the floor "
            f"and freezing the climb for {ERROR_COOLDOWN / 60:.0f}m",
            extra={"reason": "429 or 401/403"},
        )
        await _log_exit_ip()
        if inflight:
            await asyncio.gather(*list(inflight), return_exceptions=True)
        await _interruptible_sleep(ERROR_COOLDOWN, stopper, run)
    elif run.soft_pause_pending:
        run.soft_pause_pending = False
        logger.error(
            f"Backfill: recent failures crossed the back-off "
            f"threshold -- pausing {ERROR_COOLDOWN / 60:.0f}m"
        )
        if inflight:
            await asyncio.gather(*list(inflight), return_exceptions=True)
        await _interruptible_sleep(ERROR_COOLDOWN, stopper, run)
        run.backoff_window.clear()


# --- the run ---------------------------------------------------------------

async def _run(limit: int | None) -> int:
    # Dies here, before anything else, if AUDIBLE_PROXY_URL doesn't name this
    # run's own dedicated exit -- see _verify_dedicated_proxy. Applies to a
    # --limit trial exactly like the real run: a trial still calls Audible
    # for real, so it needs the same containment, not a bypass.
    _verify_dedicated_proxy()

    proxy = os.environ.get("AUDIBLE_PROXY_URL")
    logger.info(
        "Backfill: starting",
        extra={
            "proxy_host": _proxy_host_for_log(proxy),
            "limit": limit if limit is not None else "all",
            "concurrency_start": CONCURRENCY_START,
            "concurrency_ceiling": CONCURRENCY_CEILING,
            "active_hours": ACTIVE_HOURS,
        },
    )
    await _log_exit_ip()

    db_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(
        db_url,
        pool_size=CONCURRENCY_CEILING + 2,
        max_overflow=4,
        connect_args={"server_settings": {"statement_timeout": "30000"}},
    )
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    stopper = _Stopper()
    signal.signal(signal.SIGTERM, stopper.request)
    signal.signal(signal.SIGINT, stopper.request)

    sentinel = _ThrottleSentinel()
    logging.getLogger("libex").addHandler(sentinel)

    run = _Run()
    gate = _Gate(CONCURRENCY_START)
    ramp = _Ramp(gate)

    active_until = time.monotonic() + ACTIVE_HOURS * 3600
    cursor: str | None = None
    wrapped = False

    def _should_stop() -> bool:
        return stopper.stopping or run.stopping

    try:
        while not _should_stop():
            if limit is not None and run.dispatched >= limit:
                logger.info("Backfill: reached --limit, stopping")
                break

            # macro schedule: if the active window is up, pause a random spell.
            if time.monotonic() >= active_until:
                pause = random.uniform(PAUSE_MIN_HOURS, PAUSE_MAX_HOURS) * 3600
                logger.info(f"Backfill: active window done, pausing ~{pause / 3600:.1f}h")
                await _interruptible_sleep(pause, stopper, run)
                if _should_stop():
                    break
                active_until = time.monotonic() + ACTIVE_HOURS * 3600

            async with Session() as page_session:
                rows = await _read_page(page_session, cursor, CHUNK_SIZE)

            next_cursor, wrapped, done = _advance_cursor(rows, cursor, wrapped)
            if done:
                logger.info("Backfill: no more books need chapters after the wrap-around pass -- done!")
                break
            if not rows:
                cursor = next_cursor
                logger.info(
                    "Backfill: reached the end of the corpus, wrapping the "
                    "cursor back to the start for one more pass"
                )
                continue
            cursor = next_cursor
            logger.info(f"RESUME CURSOR: {cursor}")

            work = [(asin, region) for asin, region, checked in rows if checked is None]
            if not work:
                continue

            inflight: set[asyncio.Task] = set()
            for asin, region in work:
                if _should_stop():
                    break
                if limit is not None and run.dispatched >= limit:
                    break

                await gate.acquire()
                task = asyncio.create_task(
                    _dispatch_one(asin, region, Session, run, gate, ramp, sentinel, stopper)
                )
                inflight.add(task)
                task.add_done_callback(inflight.discard)
                run.dispatched += 1

                await _handle_pending_pauses(run, inflight, stopper)

                if run.stopping:
                    break

            # Unconditional drain before moving on to the next page, exactly
            # as before -- then one more pending-pause check, since a flag
            # set by whichever task finished LAST during this very drain has
            # no further loop iteration to be caught from otherwise. See
            # _handle_pending_pauses's own docstring for the full reasoning.
            if inflight:
                await asyncio.gather(*list(inflight), return_exceptions=True)
            await _handle_pending_pauses(run, inflight, stopper)
    finally:
        logging.getLogger("libex").removeHandler(sentinel)
        await engine.dispose()
        if run.abort_reason:
            logger.error("Backfill: ABORTED", extra={"reason": run.abort_reason})
        logger.info(
            "Backfill: stopped",
            extra={
                "processed": run.processed,
                "stored": run.stored,
                "no_chapters": run.none,
                "not_found": run.not_found,
                "permanent": run.permanent,
                "errors": run.errors,
                "final_concurrency": gate.limit,
                "ratchet_tripped": run.ratchet.tripped,
            },
        )
        logger.info(f"RESUME CURSOR: {cursor or '(start)'}")

    # 0: clean (finished the corpus, hit --limit, or a plain stop request).
    # 1: aborted -- a second 429/401/403 after the ratchet's floor, sustained
    # 5xx, or a NONE-rate spike.
    return 1 if run.abort_reason else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill book chapters from Audible.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N books then stop (for a dry run). Omit to process all.",
    )
    args = parser.parse_args()
    # Wire up the app's log handlers (stdout/stderr/file) -- get_logger only
    # fetches the logger; without this the handlers aren't attached and nothing
    # is emitted when run as a standalone script.
    setup_logging()
    exit_code = asyncio.run(_run(args.limit))
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
