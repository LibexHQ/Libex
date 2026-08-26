"""
Corpus repair tool. Re-fetches every book already stored and rewrites it
through the normal fetch/normalize/persist path, so a writer or normalizer
fix reaches existing rows -- the seeder never revisits a released title, so
without a pass here a fix only applies to new books. Not bulk-scraping: it
only re-reads ASINs already in the `books` table and acquires nothing new.

Walks `books` keyset-paginated on `asin`, grouping each page by region before
chunking, since a book ASIN resolves only in its own marketplace. Concurrency
ramps from REFRESH_CONCURRENCY_START on clean latency evidence up to
REFRESH_CONCURRENCY_MAX, and steps back down on degradation. Any 429, or a
sustained run of 5xx, aborts outright -- this is a one-off with no deadline,
and a fresh VPN exit is a minute of work.

RUN IT (its own container, its own AirVPN endpoint -- AUDIBLE_PROXY_URL must
name it explicitly; the run refuses to start unless its hostname contains
"refresh", see _verify_dedicated_proxy):

    docker run -d --name libex-refresh-corpus \\
      --network libex-proxy \\
      --network libex_default \\
      -e DATABASE_URL=<same as the app> \\
      -e AUDIBLE_PROXY_URL=http://libex-refresh-vpn:8888 \\
      ghcr.io/libexhq/libex:latest \\
      python -m scripts.refresh_corpus --dry-run     # prints the plan, calls nothing

Both networks are needed: libex-proxy reaches the VPN sidecar, and the app
stack's own network is the only place the `postgres` host in DATABASE_URL
resolves. Its real name is the stack's, not necessarily libex_default --
`docker inspect libex --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'`
prints both; whichever is not libex-proxy is the stack's. Repeating
--network on `docker run` needs Engine 25.0+; on older ones use docker
create, docker network connect, then docker start.

Drop --dry-run for the real run. `docker stop -t 600 libex-refresh-corpus`
finishes chunks in flight, prints the resume cursor, and exits. -t must be
STRICTLY GREATER than DRAIN_TIMEOUT_SECONDS -- a SIGTERM landing mid-drain
can run one page's drain and then the exit drain back to back, up to
DRAIN_TIMEOUT_SECONDS x 2 (~600s worst case). Docker's default 10s grace
ends in SIGKILL before a drain finishes, which rewinds the resume cursor a
page.

Restart with `--resume-from <asin>` (or REFRESH_RESUME_FROM), using the ASIN
from the last `RESUME CURSOR:` log line.

ENVIRONMENT.

    DATABASE_URL                      required. Same database the app uses.
                                       Needs the app stack's own network as
                                       well as libex-proxy; see RUN IT above.
    AUDIBLE_PROXY_URL                 required. Hostname must contain "refresh".
    REFRESH_RESUME_FROM        (unset) ASIN to resume after (exclusive).
    LOG_LEVEL                  INFO    WARNING+ drops the RESUME CURSOR line
                                       and both the 429 and 5xx aborts, which
                                       key off a WARNING-level log record.
    AXIOM_TOKEN                (unset) set to also ship logs to Axiom.
    AXIOM_DATASET               libex
    LOG_RETENTION_DAYS          7      0 keeps everything.
    CACHE_TTL                          86400   seconds until a cached book expires.
    REFRESH_PAGE_SIZE                  25000   rows per keyset page.
    REFRESH_CONCURRENCY_START          6       opening ramp rung.
    REFRESH_CONCURRENCY_MAX            48      ramp ceiling.
    REFRESH_RAMP_STEP                  6       width added per climb.
    REFRESH_RAMP_INTERVAL              150     clean chunks required per climb.
    REFRESH_LATENCY_WINDOW             60      latency samples per rolling window.
    REFRESH_DEGRADE_P95_RATIO          2.0     p95-vs-best-p95 ratio that steps
                                                the ramp down and freezes it.
    REFRESH_ABORT_5XX_WITHIN           20      5xx count that aborts the run.
    REFRESH_ABORT_5XX_WINDOW_SECONDS   120.0   window that count is measured over.
    REFRESH_ABORT_CHUNK_FAILURE_RATE   0.25    sustained chunk-failure rate that aborts.
    REFRESH_ABORT_CHUNK_FAILURE_MIN    40      chunks required before that rate is judged.
    REFRESH_DB_WRITE_CONCURRENCY       8       concurrent background persist transactions.
    REFRESH_BACKLOG_HIGH_WATER         2550    queued books above which dispatch waits;
                                                derived from CONCURRENCY_MAX, so it falls
                                                as that rises -- the run refuses to start
                                                if the derivation leaves no headroom at all.
    REFRESH_DRAIN_TIMEOUT_SECONDS      300.0   bound on waiting for the persist queue,
                                                between pages and at exit. `docker stop -t`
                                                must exceed this.
    REFRESH_PROGRESS_EVERY             100     chunks between progress lines.

Exit codes: 0 clean, 1 aborted (429, sustained 5xx, or chunk-failure rate), 2
finished without aborting but a shed batch or a drain timeout means not
everything landed.
"""

# Standard library
import argparse
import asyncio
import logging
import os
import signal
import time
from collections import deque
from contextvars import ContextVar
from typing import Any

# Third party
import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.models import Book
from app.db.session import AsyncSessionFactory, engine

# Core
from app.core.exceptions import NotFoundException
from app.core.logging import get_logger, setup_logging

# Services
from app.services.audible import books as books_service
from app.services.audible import client as audible_client
from app.services.db import persist_queue

logger = get_logger()


# ============================================================
# TUNABLES
# ============================================================

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# The bulk catalog endpoint returns at most 50 products per request. Not
# tunable: a larger value would silently return 50 anyway and drop the rest.
CHUNK_SIZE = 50

# Rows read per keyset page. Large enough that the short per-region tail
# chunk at the end of a page is negligible, small enough that a restart
# re-does a minute of work rather than an hour.
PAGE_SIZE = _env_int("REFRESH_PAGE_SIZE", 25000)

# The ramp. Opens below AUDIBLE_CONCURRENCY_LIMIT, climbs on clean evidence,
# and holds wherever it stops being clean. MAX is a default ceiling, not a
# measured one -- raise it live if the endpoint is visibly bored.
CONCURRENCY_START = _env_int("REFRESH_CONCURRENCY_START", 6)
CONCURRENCY_MAX = _env_int("REFRESH_CONCURRENCY_MAX", 48)
RAMP_STEP = _env_int("REFRESH_RAMP_STEP", 6)
RAMP_INTERVAL = _env_int("REFRESH_RAMP_INTERVAL", 150)

# Degradation. Latency is sampled per Audible request over a rolling window,
# kept separately per region -- a level's p95 is compared only against that
# region's own best. Past this ratio, the run steps back down a rung and
# stops climbing for good.
LATENCY_WINDOW = _env_int("REFRESH_LATENCY_WINDOW", 60)
DEGRADE_P95_RATIO = _env_float("REFRESH_DEGRADE_P95_RATIO", 2.0)

# Latency samples a region must contribute before the degrade check above is
# allowed to act on it. Three windows rather than one, so the baseline is a
# settled minimum rather than whichever window happened to land first --
# a single window froze a real run at CONCURRENCY_START on nothing but
# first-window jitter. This only delays the check; a genuinely degrading
# exit still trips it once the ramp knows what normal looks like.
DEGRADE_WARMUP_SAMPLES = LATENCY_WINDOW * 3

# Abort thresholds. Any 429 ends the run outright. 5xx is allowed to be noise
# up to a point, because a single upstream 503 is not a throttle.
ABORT_5XX_WITHIN = _env_int("REFRESH_ABORT_5XX_WITHIN", 20)
ABORT_5XX_WINDOW_SECONDS = _env_float("REFRESH_ABORT_5XX_WINDOW_SECONDS", 120.0)
# A chunk that failed is a chunk that did not repair its books. A handful
# across a long run is unremarkable; a sustained rate means the endpoint or
# the database is in trouble and the run should stop rather than burn through
# the corpus writing nothing.
ABORT_CHUNK_FAILURE_RATE = _env_float("REFRESH_ABORT_CHUNK_FAILURE_RATE", 0.25)
ABORT_CHUNK_FAILURE_MIN = _env_int("REFRESH_ABORT_CHUNK_FAILURE_MIN", 40)

# Concurrent background persist transactions. The application default is 2,
# sized so the seeder cannot starve the API's connection pool -- a constraint
# that doesn't exist in a dedicated container. Bounded by app/db/session.py's
# pool (pool_size 10 + max_overflow 10); 8 leaves headroom for the pager.
DB_WRITE_CONCURRENCY = _env_int("REFRESH_DB_WRITE_CONCURRENCY", 8)

# Queued-book count above which the dispatcher stops handing out new chunks.
# Derived from persist_queue's own shed limit so every chunk already in
# flight can land without reaching it. The reserve is (CONCURRENCY_MAX + 1)
# chunks, not CONCURRENCY_MAX: the dispatcher checks this water mark BEFORE
# gate.acquire(), so the chunk being checked hasn't taken a slot yet either.
# See _verify_backlog_headroom for what happens when this goes negative.
BACKLOG_HIGH_WATER = _env_int(
    "REFRESH_BACKLOG_HIGH_WATER",
    persist_queue.backlog_capacity() - (CONCURRENCY_MAX + 1) * CHUNK_SIZE,
)

# How long to wait for the persist queue to empty before giving up on it --
# used both between pages and once at exit. Bounded rather than indefinite:
# a genuinely stuck queue should stop the run, not hang the container forever.
DRAIN_TIMEOUT_SECONDS = _env_float("REFRESH_DRAIN_TIMEOUT_SECONDS", 300.0)

PROGRESS_EVERY = _env_int("REFRESH_PROGRESS_EVERY", 100)


# ============================================================
# PER-CHUNK OBSERVATION
# ============================================================

# One script-level chunk is exactly one Audible request, so a per-chunk
# record populated by the audible_get wrapper is per-request truth. Lives in
# a ContextVar so each chunk task gets its own record automatically.
_chunk_observation: ContextVar[dict[str, Any] | None] = ContextVar(
    "_refresh_chunk_observation", default=None
)


def _install_audible_observer() -> None:
    """
    Wraps the audible_get name the books service resolves, for this process
    only, with a pass-through that times the call and records its outcome.

    Observes; never changes anything -- exceptions and responses are
    unchanged. Exists because get_books_by_asins hides a failed fetch behind
    stored data, correct for a live request but the one thing this run must
    be able to see.
    """
    original = books_service.audible_get

    async def observed(region: str, path: str, params=None, extra_headers=None):
        started = time.monotonic()
        record = _chunk_observation.get()
        try:
            result = await original(region, path, params, extra_headers)
        except BaseException as exc:
            if record is not None:
                record["elapsed"] = time.monotonic() - started
                record["failed"] = True
                record["error_type"] = type(exc).__name__
            raise
        if record is not None:
            record["elapsed"] = time.monotonic() - started
            record["failed"] = False
        return result

    books_service.audible_get = observed


def _install_persist_shed_observer(run: "_Run") -> None:
    """
    Wraps persist_queue._spawn, for this process only, to detect a shed batch.

    run.books_refreshed already counted these books as repaired at fetch
    time, before persistence runs, so without this a shed batch is
    indistinguishable from a landed one. The before/after delta in
    _queued_books tells them apart: an admitted batch raises it by `books`,
    a shed one leaves it untouched.
    """
    original = persist_queue._spawn

    def observed(make_coro, books: int) -> None:
        # The private counter, not queued_books(): this watches
        # persist_queue's own internal bookkeeping to catch a shed batch by
        # its absence of effect, not asking a public question about depth.
        before = persist_queue._queued_books
        original(make_coro, books)
        if persist_queue._queued_books == before:
            run.books_shed += books

    persist_queue._spawn = observed


class _ThrottleSentinel(logging.Handler):
    """
    Watches for the throttle line audible_get emits on every 429/5xx,
    including ones a retry absorbed -- otherwise invisible to the observer
    above. Keys on the structured fields (status_code with attempts_left),
    emitted by exactly one call site, rather than message text.
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


# ============================================================
# CONCURRENCY GATE
# ============================================================

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


# ============================================================
# RAMP CONTROLLER
# ============================================================

class _RegionSignal:
    """One region's latency window, best p95, and clean streak."""

    def __init__(self) -> None:
        self.latencies: deque[float] = deque(maxlen=LATENCY_WINDOW)
        self.clean_streak = 0
        self.best_p95: float | None = None
        # Total samples ever seen, not the window's length -- the window is
        # bounded, and what the warmup needs to know is how much evidence this
        # region has contributed overall.
        self.samples = 0

    def p95(self) -> float | None:
        if len(self.latencies) < LATENCY_WINDOW:
            return None
        ordered = sorted(self.latencies)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


class _Ramp:
    """
    Climbs the shared gate on evidence from every region and steps it down
    on degradation in any one.

    The gate stays global -- every region leaves by the same VPN exit -- but
    the signal driving it is tracked per region: each gets its own latency
    window, best p95, and clean streak, so one throttled marketplace's
    evidence isn't diluted by healthy ones and a slow-but-stable region isn't
    judged against another's baseline. A region past DEGRADE_P95_RATIO
    relative to its own best steps the gate down and freezes the climb for
    the rest of the run. A step up requires every region with a full window
    to be independently clean for RAMP_INTERVAL chunks at once.
    """

    def __init__(self, gate: _Gate) -> None:
        self._gate = gate
        self._regions: dict[str, _RegionSignal] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def _signal(self, region: str) -> _RegionSignal:
        return self._regions.setdefault(region, _RegionSignal())

    async def record(self, region: str, elapsed: float | None, failed: bool) -> None:
        signal = self._signal(region)

        if failed:
            signal.clean_streak = 0
            return

        signal.clean_streak += 1
        if elapsed is not None:
            signal.latencies.append(elapsed)
            signal.samples += 1

        p95 = signal.p95()
        if p95 is None:
            return

        if signal.best_p95 is None or p95 < signal.best_p95:
            signal.best_p95 = p95

        # Warming up: let best_p95 settle before this region can freeze the
        # ramp. Climbing stays allowed -- this only gates the degrade branch.
        warming_up = signal.samples < DEGRADE_WARMUP_SAMPLES

        if not warming_up and not self._frozen and p95 > signal.best_p95 * DEGRADE_P95_RATIO:
            await self._step_down(region, signal, p95)
            return

        if not self._frozen and signal.clean_streak >= RAMP_INTERVAL:
            warm = [s for s in self._regions.values() if s.best_p95 is not None]
            if warm and all(s.clean_streak >= RAMP_INTERVAL for s in warm):
                await self._step_up()

    async def _step_up(self) -> None:
        if self._gate.limit >= CONCURRENCY_MAX:
            for signal in self._regions.values():
                signal.clean_streak = 0
            return
        new_limit = min(CONCURRENCY_MAX, self._gate.limit + RAMP_STEP)
        # Reset before the await so a concurrent record() that runs while
        # this is suspended can't independently re-satisfy "all regions
        # clean" and call _step_up() again before this climb has landed.
        for signal in self._regions.values():
            signal.clean_streak = 0
            signal.latencies.clear()
        await self._gate.set_limit(new_limit)
        logger.info("Refresh: ramping up", extra={
            "concurrency": new_limit,
            "ceiling": CONCURRENCY_MAX,
            "regions_warm": len(self._regions),
        })

    async def _step_down(self, region: str, signal: _RegionSignal, p95: float) -> None:
        self._frozen = True
        new_limit = max(CONCURRENCY_START, self._gate.limit - RAMP_STEP)
        await self._gate.set_limit(new_limit)
        best_p95 = signal.best_p95
        for s in self._regions.values():
            s.clean_streak = 0
            s.latencies.clear()
        logger.warning("Refresh: latency degraded, holding below the ceiling", extra={
            "region": region,
            "concurrency": new_limit,
            "p95_ms": round(p95 * 1000, 1),
            "best_p95_ms": round((best_p95 or 0) * 1000, 1),
            "ratio": DEGRADE_P95_RATIO,
        })


# ============================================================
# RUN STATE
# ============================================================

class _Run:
    """Counters, the abort flag, and the cursor the exit line reports."""

    def __init__(self, cursor: str | None) -> None:
        self.cursor = cursor
        self.stopping = False
        self.abort_reason: str | None = None
        self.started = time.monotonic()
        self.pages = 0
        self.chunks_done = 0
        self.chunks_failed = 0
        self.books_refreshed = 0
        self.books_not_found = 0
        self.books_shed = 0
        self.recent_failures: deque[bool] = deque(maxlen=200)

    def request_stop(self, *_args) -> None:
        if not self.stopping:
            logger.info("Refresh: stop requested, finishing the chunks in flight")
        self.stopping = True

    def abort(self, reason: str) -> None:
        if self.abort_reason is None:
            self.abort_reason = reason
        self.stopping = True

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


# ============================================================
# PAGING AND CHUNKING
# ============================================================

async def _read_page(session: AsyncSession, cursor: str | None, size: int) -> list[tuple[str, str]]:
    """
    One keyset page of (asin, region), ordered by the primary key.

    `asin > cursor` on the primary key is an index range scan with no sort, so
    the cost of a page does not grow with how far into the corpus it is.
    """
    stmt = select(Book.asin, Book.region).order_by(Book.asin).limit(size)
    if cursor is not None:
        stmt = stmt.where(Book.asin > cursor)
    result = await session.execute(stmt)
    return [(row[0], row[1]) for row in result.all()]


def _chunks_for_page(rows: list[tuple[str, str]], size: int) -> list[tuple[str, list[str]]]:
    """
    Splits a page into (region, asins) chunks, grouped by region first.

    Region grouping is the whole point and is done here rather than in the
    query: a book ASIN resolves only in its own marketplace, so a chunk mixing
    regions would 404 most of what it asked for. Duplicate ASINs within a
    region are collapsed — get_books_by_asins does the same thing internally,
    and doing it here keeps the chunk count honest.

    Returns chunks in region order, each of at most `size` ASINs. The last
    chunk of each region in a page is usually short; that raggedness is the
    price of not carrying residuals across a page boundary, where they would
    be lost on a restart.
    """
    by_region: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for asin, region in rows:
        bucket = by_region.setdefault(region, [])
        marks = seen.setdefault(region, set())
        if asin in marks:
            continue
        marks.add(asin)
        bucket.append(asin)

    chunks: list[tuple[str, list[str]]] = []
    for region in sorted(by_region):
        asins = by_region[region]
        for start in range(0, len(asins), size):
            chunks.append((region, asins[start:start + size]))
    return chunks


# ============================================================
# THE WORK
# ============================================================

async def _refresh_chunk(region: str, asins: list[str], run: _Run, ramp: _Ramp) -> None:
    """
    Re-fetches one chunk and lets the normal path persist it.

    get_books_by_asins is the only write route: it fetches, normalizes and
    hands the result to the background writer, so every writer fix applies
    here automatically and this script has no persistence code of its own.

    use_cache is False, explicitly and non-negotiably. With it True the
    function would answer from the very rows this pass exists to repair and
    never reach Audible at all.
    """
    observation: dict[str, Any] = {}
    _chunk_observation.set(observation)

    failed = False
    try:
        async with AsyncSessionFactory() as session:
            books = await books_service.get_books_by_asins(
                asins, region, session, use_cache=False
            )
        # A chunk whose Audible request failed still returns rows — the stored
        # ones, via the service's own fallback. The observer is what tells the
        # two apart, so the count only moves when the fetch actually happened.
        if observation.get("failed"):
            failed = True
        else:
            run.books_refreshed += len(books)
            run.books_not_found += max(0, len(asins) - len(books))
    except NotFoundException:
        # Terminal for this chunk and never a retry signal: the ASINs are not
        # in this marketplace. Only a single-ASIN tail chunk can reach here,
        # since the bulk endpoint answers 200 with an empty products array
        # rather than 404. Not counted as a failure.
        run.books_not_found += len(asins)
    except Exception as exc:
        failed = True
        logger.warning("Refresh: chunk failed", extra={
            "region": region,
            "asins": len(asins),
            "error_type": type(exc).__name__,
            "sqlstate": getattr(getattr(exc, "orig", None), "sqlstate", None),
        })

    run.chunks_done += 1
    if failed:
        run.chunks_failed += 1
    run.recent_failures.append(failed)
    await ramp.record(region, observation.get("elapsed"), failed)


async def _wait_for_backlog(run: _Run) -> None:
    """
    Holds the dispatcher while the persist queue is deep.

    Past its bound the queue sheds, and a shed book is one this pass counts as
    repaired and did not repair. Waiting here is the difference between a fast
    run and a fast run that lies about what it did.
    """
    waited = False
    while persist_queue.queued_books() > BACKLOG_HIGH_WATER and not run.stopping:
        if not waited:
            logger.info("Refresh: waiting on the persist backlog", extra={
                "queued_books": persist_queue.queued_books(),
                "high_water": BACKLOG_HIGH_WATER,
            })
            waited = True
        await asyncio.sleep(0.5)


async def _drain_persist_queue(timeout: float) -> bool:
    """
    Waits for the persist queue to empty. Returns whether it actually did.

    Used both to make the per-page resume cursor honest -- persistence is
    fire-and-forget, so a page's fetches all completing is not the same as
    its writes landing, and the cursor must not claim a page whose books
    aren't in the database yet -- and once more at exit, where the same
    question decides whether the shutdown was clean or lossy.
    """
    started = time.monotonic()
    while persist_queue.queued_books() > 0:
        if time.monotonic() - started > timeout:
            return False
        logger.info("Refresh: draining the persist queue", extra={
            "queued_books": persist_queue.queued_books(),
        })
        await asyncio.sleep(2.0)
    return True


def _check_abort(run: _Run, sentinel: _ThrottleSentinel) -> None:
    """Every condition that ends the run rather than slowing it down."""
    if sentinel.throttled:
        run.abort(f"Audible returned {sentinel.throttled} throttled response(s)")
        return
    if sentinel.sustained_server_errors:
        run.abort(
            f"{len(sentinel.server_errors)} upstream 5xx within "
            f"{ABORT_5XX_WINDOW_SECONDS:.0f}s"
        )
        return
    recent = run.recent_failures
    if len(recent) >= ABORT_CHUNK_FAILURE_MIN:
        rate = sum(recent) / len(recent)
        if rate >= ABORT_CHUNK_FAILURE_RATE:
            run.abort(f"chunk failure rate {rate:.0%} over the last {len(recent)} chunks")


def _log_progress(run: _Run, gate: _Gate, ramp: _Ramp, remaining_books: int) -> None:
    rate = run.chunks_done / run.elapsed if run.elapsed > 0 else 0.0
    books_per_second = run.books_refreshed / run.elapsed if run.elapsed > 0 else 0.0
    eta_seconds = remaining_books / books_per_second if books_per_second > 0 else 0.0
    logger.info("Refresh: progress", extra={
        "pages": run.pages,
        "chunks_done": run.chunks_done,
        "chunks_failed": run.chunks_failed,
        "books_refreshed": run.books_refreshed,
        "books_not_found": run.books_not_found,
        "books_shed": run.books_shed,
        "concurrency": gate.limit,
        "ramp_frozen": ramp.frozen,
        "queued_books": persist_queue.queued_books(),
        "chunks_per_second": round(rate, 2),
        "elapsed_minutes": round(run.elapsed / 60, 1),
        "eta_minutes": round(eta_seconds / 60, 1),
    })


# ============================================================
# PROCESS LIMITS
# ============================================================

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
    contains "refresh" -- protects the shared exit from
    _raise_process_limits' 48-wide ceiling. Checked against the hostname
    only, since the real proxy value may carry embedded credentials and must
    never reach a log line or exception message. Logged before the
    SystemExit, since SystemExit alone never reaches the log handlers.
    """
    proxy = os.environ.get("AUDIBLE_PROXY_URL", "")
    host = _proxy_host_for_log(proxy) if proxy else ""
    if not proxy or "refresh" not in host:
        detail = f"host {host!r}" if proxy else "unset"
        logger.error(
            "Refresh: refusing to start, AUDIBLE_PROXY_URL does not name "
            "a refresh-dedicated exit",
            extra={"proxy_host": host or "unset", "proxy_configured": bool(proxy)},
        )
        raise SystemExit(
            f"AUDIBLE_PROXY_URL ({detail}) does not name a "
            f"refresh-dedicated exit. Refusing to raise concurrency to "
            f"{CONCURRENCY_MAX} against what may be the shared production "
            "proxy -- point this at the dedicated refresh exit before starting."
        )


def _verify_backlog_headroom() -> None:
    """
    Refuses to start when BACKLOG_HIGH_WATER leaves no room to ever admit a
    chunk. At REFRESH_CONCURRENCY_MAX = 99 (unbounded above), the default
    derivation already consumes the whole backlog_capacity() in reserve,
    going negative past that -- and _wait_for_backlog would then block
    forever on a water mark it can never satisfy.

    Refuses rather than clamping with max(...): a clamp would leave the
    guard present but silently useless. Requires at least one full chunk of
    headroom, not merely a positive water mark.
    """
    if BACKLOG_HIGH_WATER < CHUNK_SIZE:
        reserve = (CONCURRENCY_MAX + 1) * CHUNK_SIZE
        raise SystemExit(
            f"BACKLOG_HIGH_WATER={BACKLOG_HIGH_WATER} leaves no room to ever "
            f"admit a chunk (backlog_capacity()={persist_queue.backlog_capacity()} "
            f"minus the derived (CONCURRENCY_MAX + 1) x CHUNK_SIZE reserve "
            f"({reserve}) at REFRESH_CONCURRENCY_MAX={CONCURRENCY_MAX}, unless "
            "REFRESH_BACKLOG_HIGH_WATER was set explicitly). The dispatcher "
            "would block forever waiting on a water mark it can never "
            "satisfy. Lower REFRESH_CONCURRENCY_MAX, or raise "
            "REFRESH_BACKLOG_HIGH_WATER explicitly and accept the reduced "
            "in-flight cushion, before starting."
        )


def _raise_process_limits() -> None:
    """
    Widens the two application limits that would otherwise cap this run
    below anything the ramp could reach -- in this process only, before
    anything has built a client, a semaphore or a session.

    Both are sized for the API process; neither constraint applies to a
    dedicated one-off container, so they're rebound here rather than made
    environment-driven in application code for a script that gets deleted
    after one night. The asserts are the safety: both application objects
    are built lazily on first use, so a later caller would otherwise
    silently get the old limits.
    """
    assert audible_client._audible_client is None, "Audible client already built"
    assert audible_client._audible_semaphore is None, "Audible semaphore already built"
    assert persist_queue._bg_write_semaphore is None, "Persist semaphore already built"

    audible_client.AUDIBLE_CONCURRENCY_LIMIT = CONCURRENCY_MAX
    audible_client._AUDIBLE_POOL_LIMITS = httpx.Limits(
        max_connections=CONCURRENCY_MAX,
        max_keepalive_connections=CONCURRENCY_MAX,
        keepalive_expiry=120.0,
    )
    persist_queue._BG_WRITE_CONCURRENCY_LIMIT = DB_WRITE_CONCURRENCY

    logger.info("Refresh: process limits set", extra={
        "audible_concurrency_ceiling": CONCURRENCY_MAX,
        "db_write_concurrency": DB_WRITE_CONCURRENCY,
    })


# ============================================================
# THE RUN
# ============================================================

async def _remaining_books(session: AsyncSession, cursor: str | None) -> int:
    stmt = select(func.count()).select_from(Book)
    if cursor is not None:
        stmt = stmt.where(Book.asin > cursor)
    return int((await session.execute(stmt)).scalar_one())


async def _dry_run(cursor: str | None, pages: int) -> None:
    """
    Prints what the pass would do, without a single Audible call.

    Proves the two things most worth proving before spending an endpoint: that
    the page really does split by region, and that each region really is
    chunked separately at 50.
    """
    async with AsyncSessionFactory() as session:
        total = await _remaining_books(session, cursor)
        logger.info("Refresh dry run: corpus", extra={
            "books_remaining": total,
            "cursor": cursor or "(start)",
            "page_size": PAGE_SIZE,
            "chunk_size": CHUNK_SIZE,
        })
        for page_number in range(pages):
            rows = await _read_page(session, cursor, PAGE_SIZE)
            if not rows:
                logger.info("Refresh dry run: end of corpus")
                return
            chunks = _chunks_for_page(rows, CHUNK_SIZE)
            per_region: dict[str, int] = {}
            for region, asins in chunks:
                per_region[region] = per_region.get(region, 0) + len(asins)
            logger.info("Refresh dry run: page", extra={
                "page": page_number + 1,
                "rows": len(rows),
                "chunks": len(chunks),
                "regions": len(per_region),
                "books_per_region": per_region,
                "first_asin": rows[0][0],
                "last_asin": rows[-1][0],
            })
            cursor = rows[-1][0]
        logger.info(f"RESUME CURSOR: {cursor}")


async def _run(cursor: str | None) -> int:
    _verify_dedicated_proxy()
    _verify_backlog_headroom()
    _raise_process_limits()
    _install_audible_observer()

    sentinel = _ThrottleSentinel()
    logging.getLogger("libex").addHandler(sentinel)

    run = _Run(cursor)
    _install_persist_shed_observer(run)
    signal.signal(signal.SIGTERM, run.request_stop)
    signal.signal(signal.SIGINT, run.request_stop)

    gate = _Gate(CONCURRENCY_START)
    ramp = _Ramp(gate)

    async with AsyncSessionFactory() as session:
        remaining = await _remaining_books(session, cursor)

    logger.info("Refresh: starting", extra={
        "books_remaining": remaining,
        "resume_from": cursor or "(start)",
        "concurrency_start": CONCURRENCY_START,
        "concurrency_max": CONCURRENCY_MAX,
        "ramp_step": RAMP_STEP,
        "ramp_interval": RAMP_INTERVAL,
        "page_size": PAGE_SIZE,
        "proxy": bool(os.environ.get("AUDIBLE_PROXY_URL")),
    })

    inflight: set[asyncio.Task] = set()

    async def _dispatch(region: str, asins: list[str]) -> None:
        try:
            await _refresh_chunk(region, asins, run, ramp)
        finally:
            await gate.release()

    try:
        while not run.stopping:
            async with AsyncSessionFactory() as session:
                rows = await _read_page(session, run.cursor, PAGE_SIZE)
            if not rows:
                logger.info("Refresh: end of corpus reached")
                break

            page_end = rows[-1][0]
            chunks = _chunks_for_page(rows, CHUNK_SIZE)
            logger.info("Refresh: page", extra={
                "page": run.pages + 1,
                "rows": len(rows),
                "chunks": len(chunks),
                "regions": len({region for region, _ in chunks}),
            })

            for region, asins in chunks:
                if run.stopping:
                    break
                await _wait_for_backlog(run)
                if run.stopping:
                    break
                await gate.acquire()
                task = asyncio.create_task(_dispatch(region, asins))
                inflight.add(task)
                task.add_done_callback(inflight.discard)

                _check_abort(run, sentinel)
                if run.chunks_done and run.chunks_done % PROGRESS_EVERY == 0:
                    _log_progress(run, gate, ramp, remaining - run.books_refreshed)

            # Every chunk of the page has to be dispatched, fetched, AND
            # persisted before the cursor moves. Awaiting `inflight` alone
            # only proves the fetches finished -- persistence runs in fire-
            # and-forget background tasks of its own, so the queue is drained
            # here too; advancing the cursor before either would checkpoint
            # past books this run never actually finished refreshing. If the
            # page's dispatch loop broke early on a stop request, or the
            # drain times out, the cursor stays at the previous page rather
            # than claim one that never fully landed -- a restart re-does at
            # most that one page.
            if inflight:
                await asyncio.gather(*list(inflight), return_exceptions=True)

            _check_abort(run, sentinel)
            if run.abort_reason:
                break

            page_dispatched = not run.stopping
            page_landed = page_dispatched and await _drain_persist_queue(DRAIN_TIMEOUT_SECONDS)

            if page_landed:
                run.pages += 1
                run.cursor = page_end
                logger.info(f"RESUME CURSOR: {run.cursor}")
            elif page_dispatched:
                # Every chunk fetched, but persistence didn't drain within
                # DRAIN_TIMEOUT_SECONDS -- not a blip this run can wait out
                # safely, since dispatching the next page on top of an
                # already-backed-up queue only makes it worse. Abort rather
                # than re-fetch the same page forever with the cursor pinned.
                run.abort("persist queue failed to drain within DRAIN_TIMEOUT_SECONDS")
                logger.warning("Refresh: page not confirmed complete, cursor held", extra={
                    "cursor": run.cursor or "(start)",
                })
            else:
                logger.info("Refresh: stop requested mid-page, cursor held at the previous boundary", extra={
                    "cursor": run.cursor or "(start)",
                })
    finally:
        if inflight:
            await asyncio.gather(*list(inflight), return_exceptions=True)

        # The persist queue is fire-and-forget: exiting here with tasks still
        # queued drops the writes this pass exists to make.
        drained = await _drain_persist_queue(DRAIN_TIMEOUT_SECONDS)

        await engine.dispose()
        logging.getLogger("libex").removeHandler(sentinel)

        if run.abort_reason:
            logger.error("Refresh: ABORTED", extra={"reason": run.abort_reason})
        if not drained:
            logger.error("Refresh: exiting with the persist queue still non-empty", extra={
                "queued_books": persist_queue.queued_books(),
            })
        if run.books_shed:
            logger.warning("Refresh: books were shed during persistence and were not repaired", extra={
                "books_shed": run.books_shed,
            })
        logger.info("Refresh: stopped", extra={
            "pages": run.pages,
            "chunks_done": run.chunks_done,
            "chunks_failed": run.chunks_failed,
            "books_refreshed": run.books_refreshed,
            "books_not_found": run.books_not_found,
            "books_shed": run.books_shed,
            "throttled_responses": sentinel.throttled,
            "final_concurrency": gate.limit,
            "elapsed_minutes": round(run.elapsed / 60, 1),
            "clean_exit": drained and run.abort_reason is None and run.books_shed == 0,
        })
        logger.info(f"RESUME CURSOR: {run.cursor or '(start)'}")

    if run.abort_reason:
        return 1
    if run.books_shed or not drained:
        return 2
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-fetch and rewrite every stored book, region by region."
    )
    parser.add_argument(
        "--resume-from",
        default=os.environ.get("REFRESH_RESUME_FROM") or None,
        help="ASIN to resume after, as printed by the RESUME CURSOR line.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the paging and per-region chunking plan. Calls nothing.",
    )
    parser.add_argument(
        "--dry-run-pages",
        type=int,
        default=2,
        help="How many pages --dry-run plans before stopping.",
    )
    args = parser.parse_args()

    # get_logger only fetches the logger. Without this the handlers are never
    # attached and a standalone script emits nothing at all.
    setup_logging()

    if args.dry_run:
        asyncio.run(_dry_run(args.resume_from, args.dry_run_pages))
        return

    # 0: clean. 1: aborted (throttled, sustained 5xx, or chunk failure rate).
    # 2: finished without aborting but did not land everything it claimed --
    # a shed batch, or a persist drain that timed out. Distinct from 1 so a
    # supervisor can tell "stopped for its own protection" apart from
    # "finished, but check the log for what didn't make it."
    exit_code = asyncio.run(_run(args.resume_from))
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
