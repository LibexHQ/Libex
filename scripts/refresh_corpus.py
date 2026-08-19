"""
Corpus repair tool. Kept, not spent.

Reach for this whenever a writer or normalizer fix lands: it only repairs rows
written after it deploys, and the seeder never revisits a released title, so
without a pass here the existing corpus keeps the old value forever. Last run
2026-08-18 for is_vvab -- 1.1M books, 105 minutes, clean.

Re-fetches every book already stored and rewrites it through the normal
fetch/normalize/persist path, so fields that were never written, or that have
drifted since the row was first stored, are repaired in place.

WHY THIS IS NOT BULK-SCRAPING. The no-bulk-scraping rule is about seeding a
catalog Libex does not have — walking Audible's catalog to acquire records.
This walks Libex's own `books` table and re-reads only ASINs already stored,
which every one of those rows was legitimately fetched for at least once
already. It acquires nothing new: an ASIN that is not in the table is never
requested, and the pass ends when the table ends.

PRECONDITION. Run only after the writer fix this pass exists to repair is
merged and deployed — the script hands normalized books to the same writer
the API uses and writes nothing itself, so running it against an undeployed
fix spends the exit budget rewriting every row with the field still unset.

HOW IT WALKS THE CORPUS.
- `books.asin` is the table's sole PRIMARY KEY, and `region` is a plain column
  with no index on it. So the walk is keyset-paginated on `asin` alone —
  an index-ordered range scan, one seek per page — and NOT on (region, asin),
  which has no index to serve it and would cost a full scan plus a top-N sort
  of 1.1M rows on every page. Ordering on the primary key is unique and total,
  so the cursor is exactly as stable as the composite would have been.
- Region is grouped WITHIN each page, never assumed. A page is read as
  (asin, region) pairs, split by region, and each region's ASINs chunked
  separately, because a book ASIN resolves only in its own marketplace: the
  same title is a different ASIN in every region. A pass that chunked across
  regions, or passed one region for everything, would 404 most of what it
  asked for and silently repair only the part of the corpus that happened to
  match.
- Pages are large (REFRESH_PAGE_SIZE, default 25000) so that the ragged last
  chunk each region contributes per page is a rounding error rather than a
  cost: at 25000 rows a page produces at most eleven short chunks against
  roughly five hundred full ones. Residual ASINs are deliberately NOT carried
  across a page boundary — that would make the page cursor a lie, since a
  crash would skip whatever was being carried.

CHECKPOINTING AND RESTART. The cursor only advances past a page once every
chunk dispatched from it has both been fetched AND had its background write
finish running — not merely queued for it, since the persist queue is
fire-and-forget and a chunk's write can still be in flight after its fetch
returns. That is a guarantee the write ran to completion, not that every row
it touched landed: a book Postgres permanently rejects fails inside the
writer's own per-book catch and is logged rather than raised, so a chunk can
finish, and the cursor can advance past it, with one row still unwritten. It
is written
to the log at every page boundary and again on exit, on its own line, prefixed
`RESUME CURSOR:`. A stop request (SIGTERM/SIGINT) that arrives mid-page, or a
persist drain that doesn't finish, leaves the cursor at the previous page
boundary rather than claiming a page whose dispatch or writes never finished.
Restart with `--resume-from <asin>` (or REFRESH_RESUME_FROM) and the walk
continues from there. There is no state file and no state table on purpose:
keyset ordering on the primary key is stable, so a single scalar is the entire
state, and a one-off container that gets deleted afterwards should not leave a
row behind in a schema that will outlive it. The cost of that choice is
bounded — a restart re-does at most one page, which is a minute or two of work.

PACING. It opens conservatively and climbs on evidence, rather than starting
wide. A brand-new exit IP whose first packets are dozens of simultaneous TLS
handshakes to api.audible.com is the shape that trips an edge heuristic, and
the endpoint is the one thing this run cannot replace cheaply mid-flight. So
concurrency starts at REFRESH_CONCURRENCY_START, climbs by REFRESH_RAMP_STEP
every REFRESH_RAMP_INTERVAL clean chunks, and holds at whatever level stops
being clean, capped by REFRESH_CONCURRENCY_MAX. The only measured evidence
available is a ladder that ran 250 concurrent against Audible with zero 429s
and zero 5xx — but on a DIRECT path, not through a VPN exit. That makes the
upstream the known quantity and the exit the unmeasured one, which is exactly
why the ramp reads the exit's own behaviour instead of trusting the ladder.
The concurrency gate is global — one exit serves every region — but the
latency signal that drives it is tracked per region, so a throttled
marketplace's evidence isn't diluted by ten healthy ones and doesn't drag the
climb down for them either. See _Ramp's own docstring for the mechanics.

DEGRADATION VERSUS THROTTLING — they are different signals and get different
answers.
- Degradation (a region's latency climbing relative to its own best) means
  "you are at the rate". It stops the climb and steps back down one rung.
  Backing off here is what lets the run finish fast.
- A 429, at any level, in any region, aborts the run. So does a sustained
  run of 5xx. Pushing through a throttle wastes the endpoint and repairs
  nothing; this is a one-off with no deadline, and a fresh AirVPN exit is a
  minute of work. The exit line carries the cursor, so a restart on a new
  endpoint loses at most one page.

HOW FAILURE IS DETECTED AT ALL. get_books_by_asins does not raise on a
throttled chunk — by design, it falls back to the stored rows and returns
something that looks exactly like a successful refresh. That is correct
behaviour for a live request and useless for this run, which needs to know
whether the row it just handled was actually re-read. Two observers cover it,
neither of which changes any application behaviour:
- audible_get is wrapped for the life of this process with a pass-through that
  records each request's latency and outcome into a per-chunk ContextVar. One
  chunk is exactly one Audible request, so this gives per-chunk truth: a chunk
  that fell back to the database is counted as failed, not repaired.
- A logging handler watches for the throttle line audible_get emits on every
  429 and 5xx it sees, including ones a retry then absorbed — those never
  become an exception and are otherwise invisible. It keys on the structured
  fields (status_code together with attempts_left), which that one call site
  is the only emitter of, rather than on message text.

BACKPRESSURE. Persistence is a background queue with a bounded backlog; past
that bound it SHEDS, dropping books silently. In normal API traffic the queue
drains far faster than requests can fill it, so the bound is never approached
— but this run's entire purpose is to fill it as fast as an event loop can,
and a shed book here is a book the pass reports as repaired and did not
repair. So the dispatcher waits whenever the queued-book count is above
BACKLOG_HIGH_WATER, derived from persist_queue's own shed limit (see that
constant's own comment) so every chunk already in flight can land without
crossing it. A shed that gets through anyway is still counted and reported in
the exit summary, never silently absorbed into a clean-looking run.

RUN IT (its own container, its own AirVPN endpoint — AUDIBLE_PROXY_URL must
name it explicitly; the run refuses to start against an unset or
production-looking proxy, see _verify_dedicated_proxy):

    docker run -d --name libex-refresh-corpus \\
      --network libex-proxy \\
      -e DATABASE_URL=<same as the app> \\
      -e AUDIBLE_PROXY_URL=http://libex-refresh-vpn:8888 \\
      ghcr.io/libexhq/libex:latest \\
      python scripts/refresh_corpus.py --dry-run     # prints the plan, calls nothing

Drop --dry-run for the real run. `docker stop -t 300 libex-refresh-corpus`
(matching or exceeding DRAIN_TIMEOUT_SECONDS; docker's own default 10s grace
ends in SIGKILL before a page's own drain can finish, and the run falls back
to the previous page's resume cursor rather than the current one) finishes
the chunks in flight, prints the resume cursor, and exits.
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


# The bulk catalog endpoint returns at most 50 products per request, which is
# what books.py chunks its own hydration at. Not a rate knob and not tunable:
# a larger value would silently return 50 anyway and drop the rest.
CHUNK_SIZE = 50

# Rows read per keyset page. Large enough that the short chunk each region
# contributes at the end of a page is negligible, small enough that a restart
# re-does a minute of work rather than an hour. 25000 rows is about 4MB of
# (asin, region) tuples.
PAGE_SIZE = _env_int("REFRESH_PAGE_SIZE", 25000)

# The ramp. Opens below the API's own per-process steady-state fan-out width
# (10, see AUDIBLE_CONCURRENCY_LIMIT), climbs on clean evidence, and holds
# wherever it stops being clean. MAX is a default ceiling, not a measured one:
# 48 is roughly a fifth of the largest concurrency measured clean anywhere,
# and that measurement was not through a VPN exit. Raise it live if the
# endpoint is visibly bored.
CONCURRENCY_START = _env_int("REFRESH_CONCURRENCY_START", 6)
CONCURRENCY_MAX = _env_int("REFRESH_CONCURRENCY_MAX", 48)
RAMP_STEP = _env_int("REFRESH_RAMP_STEP", 6)
RAMP_INTERVAL = _env_int("REFRESH_RAMP_INTERVAL", 150)

# Degradation. Latency is sampled per Audible request over a rolling window,
# kept separately per region -- a level's p95 is compared against the best
# p95 that SAME region has recorded, never pooled across regions. Past this
# ratio in any one region, the run steps back down a rung and stops climbing
# for good — that region has told us where the shared exit's rate is.
LATENCY_WINDOW = _env_int("REFRESH_LATENCY_WINDOW", 60)
DEGRADE_P95_RATIO = _env_float("REFRESH_DEGRADE_P95_RATIO", 2.0)

# Latency samples a region must contribute before the degrade check above is
# allowed to act on it.
#
# Measured on the first live pass, 2026-08-18: the ramp froze at the opening
# rung 1.3 minutes in, with no exit problem at all. best_p95 takes any new
# minimum, so the first full window sets the bar -- and consecutive early
# windows vary widely. The samples that froze that run were 1104ms, 546ms,
# 469ms, 663ms and 795ms: 1104/469 is 2.35, past the ratio, on nothing but
# ordinary jitter. The freeze is permanent, so a run that trips it spends its
# whole life at CONCURRENCY_START.
#
# Three windows rather than one, so the baseline is a settled minimum rather
# than whichever window happened to land first. It only delays the check --
# a genuinely degrading exit still trips it, just after the ramp has seen
# enough to know what normal looks like.
DEGRADE_WARMUP_SAMPLES = _env_int("REFRESH_DEGRADE_WARMUP_SAMPLES", LATENCY_WINDOW * 3)

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
# sized so the seeder cannot starve the API's connection pool — a constraint
# that does not exist in a dedicated container whose pool serves nothing else.
# Bounded by that pool: app/db/session.py sizes it at pool_size 10 plus
# max_overflow 10, so 8 writers leave headroom for the pager and for a chunk
# that falls back to a database read.
DB_WRITE_CONCURRENCY = _env_int("REFRESH_DB_WRITE_CONCURRENCY", 8)

# Queued-book count above which the dispatcher stops handing out new chunks.
# Derived from persist_queue's own shed limit rather than restated, so every
# chunk already in flight (at most CONCURRENCY_MAX x CHUNK_SIZE books) can
# land without reaching it. CONCURRENCY_MAX is env-tunable, so a hardcoded
# margin here would re-break the moment it's raised.
#
# The reserve is (CONCURRENCY_MAX + 1) chunks, not CONCURRENCY_MAX: the
# dispatcher checks this water mark BEFORE gate.acquire() (see
# _wait_for_backlog's call site), so at the instant a check passes, up to
# CONCURRENCY_MAX chunks can already be in flight and the chunk being checked
# is additional to them — it has not acquired a gate slot yet. Reserving only
# CONCURRENCY_MAX chunks' worth left that one extra chunk's books able to push
# the queue past backlog_capacity() before _spawn's own shed check ever saw
# them coming. See _verify_backlog_headroom for what happens when this
# arithmetic goes negative.
BACKLOG_HIGH_WATER = _env_int(
    "REFRESH_BACKLOG_HIGH_WATER",
    persist_queue.backlog_capacity() - (CONCURRENCY_MAX + 1) * CHUNK_SIZE,
)

# How long to wait for the persist queue to empty before giving up on it --
# used both between pages (so the resume cursor never advances past writes
# that haven't landed) and once at exit. Bounded rather than indefinite: a
# genuinely stuck queue should stop the run, not hang the container forever.
DRAIN_TIMEOUT_SECONDS = _env_float("REFRESH_DRAIN_TIMEOUT_SECONDS", 300.0)

PROGRESS_EVERY = _env_int("REFRESH_PROGRESS_EVERY", 100)


# ============================================================
# PER-CHUNK OBSERVATION
# ============================================================

# One script-level chunk is exactly one Audible request, so a per-chunk record
# populated by the audible_get wrapper is per-request truth. It lives in a
# ContextVar because asyncio copies the current context when a task is
# created, so each chunk task gets its own record with nothing threaded
# through get_books_by_asins' signature.
_chunk_observation: ContextVar[dict[str, Any] | None] = ContextVar(
    "_refresh_chunk_observation", default=None
)


def _install_audible_observer() -> None:
    """
    Wraps the audible_get name the books service resolves, for this process
    only, with a pass-through that times the call and records its outcome.

    Observes; never changes anything. Every exception is re-raised unchanged
    and every successful response is returned unchanged, so the books service
    behaves byte for byte as it does in the API. It exists because
    get_books_by_asins deliberately hides a failed fetch behind stored data —
    correct for a live request, and the one thing this run must be able to
    see.
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

    _spawn is the one place a background write is admitted or dropped, and it
    returns nothing either way -- run.books_refreshed already counted these
    books as repaired at fetch time (see _refresh_chunk), before persistence
    ever runs, so without this a shed batch is indistinguishable from a
    landed one in every line this script emits. The before/after delta in
    _queued_books is what tells them apart: an admitted batch raises it by
    `books`, a shed one leaves it untouched (persist_queue._record_shed runs
    and _spawn returns before touching the counter).
    """
    original = persist_queue._spawn

    def observed(make_coro, books: int) -> None:
        # Deliberately the private counter, not queued_books(): this observer
        # is watching persist_queue's own internal bookkeeping (the same
        # variable _spawn itself mutates) to catch a shed batch by its
        # absence of effect, not asking the module a legitimate public
        # question about queue depth the way every other read site here
        # does. Reading the accessor here would read the same value while
        # pretending this call site were the same kind of caller as those.
        before = persist_queue._queued_books
        original(make_coro, books)
        if persist_queue._queued_books == before:
            run.books_shed += books

    persist_queue._spawn = observed


class _ThrottleSentinel(logging.Handler):
    """
    Watches for the throttle line audible_get emits on every 429 and 5xx it
    receives, including the ones a retry then absorbed.

    An absorbed 429 never becomes an exception and never reaches the observer
    above, so this is the only place it is visible — and an absorbed 429 is
    precisely the early warning worth stopping on. It keys on the structured
    fields rather than the message text: status_code together with
    attempts_left is emitted by exactly one call site in the codebase (the
    retry branch of audible_get), which makes the coupling a field contract
    rather than a string match.

    Records only counts and timestamps. Nothing from the log record is
    retained or re-emitted.
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
    A concurrency gate whose limit moves while tasks are waiting on it.

    asyncio.Semaphore cannot shrink — releasing extra permits grows it, but
    nothing takes permits back — and the ramp needs to step down as readily
    as it steps up. A condition variable over an active count does both, and
    a step down takes effect as tasks finish rather than by cancelling work
    already in flight.
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
    Climbs the shared gate on evidence from every region and steps it down on
    degradation in any one.

    The gate stays global -- every region leaves by the same VPN exit, and
    that exit is the ceiling being protected -- but the signal driving it is
    tracked per region: each gets its own latency window, its own best p95,
    and its own clean streak, so a throttled marketplace's evidence isn't
    diluted by ten healthy ones' samples, and a slow-but-stable region (judged
    only against its own baseline, never another's) doesn't drag the climb
    down for everyone either. A region past DEGRADE_P95_RATIO relative to its
    own best steps the gate down and freezes the climb for the rest of the
    run -- one bad region is enough, matching the single-stream design this
    replaces. A step up requires every region that has reported enough
    samples to judge (its window is full) to be independently clean for
    RAMP_INTERVAL chunks at once; a region with too little traffic to fill a
    window contributes to neither decision. Latency is the signal because it
    moves before errors do -- an exit at its rate answers more slowly for a
    while before it answers with a status code.

    What this does NOT measure: TLS handshake time on its own, which would
    need a hook inside the HTTP client. The end-to-end request time it samples
    includes it, so a handshake slowdown shows up here — just not separably.
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

        # Warming up: keep letting best_p95 settle toward a real minimum, but
        # do not let this region freeze the ramp yet. Climbing stays allowed,
        # which is why this gates only the degrade branch below rather than
        # returning -- a cold exit that is genuinely fast should not be held
        # at the opening rung waiting for permission to leave it.
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
        # Reset before the await, not after, mirroring _step_down's own
        # self._frozen = True as its first statement. set_limit acquires the
        # gate's shared condition variable -- the same lock every concurrent
        # chunk contends on through gate.acquire()/release() -- so this await
        # genuinely suspends under load. A concurrent record() that runs
        # while it's suspended must not still see every region as clean, or
        # it independently re-satisfies the same "all warm regions clean"
        # check and calls _step_up() again before this climb has even
        # landed, stacking several climbs onto what should have been one
        # (reproduced: 30 concurrent record() calls against a contended gate
        # produced 30 _step_up() calls instead of 1). Nothing yields between
        # reading the pre-reset state and applying it here, so no other
        # decision can act on a stale streak -- the same guarantee
        # self._frozen already gives _step_down.
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

def _verify_dedicated_proxy() -> None:
    """
    Refuses to start unless AUDIBLE_PROXY_URL both is set and names this run's
    own exit, never the shared one _raise_process_limits' 48-wide ceiling
    exists to protect.

    Checked against the hostname, not a literal URL, because the real
    production proxy value is infrastructure this app never carries in
    source and has no secret to compare against. "refresh" in the hostname is
    the convention this script's own module docstring and RUN IT example
    already commit to (libex-refresh-vpn), the same way
    scripts/backfill_chapters.py names its own dedicated exit
    (libex-backfill-vpn) -- an operator who leaves the variable unset, or
    reuses the shared value, fails this on hostname alone, before a single
    request goes out or a limit gets raised.

    The failure message names the hostname only, never the full value:
    httpx's proxy= accepts credentials embedded in the URL
    (http://user:pass@host:port) and nothing in Settings forbids
    AUDIBLE_PROXY_URL being configured that way, so the one path that fires
    on operator misconfiguration must not be the one that echoes back
    whatever the operator typed, including a possible credential. The
    hostname is what "refresh" is actually checked against and is enough to
    tell the operator what failed; they already know what they set.
    """
    proxy = os.environ.get("AUDIBLE_PROXY_URL", "")
    host = httpx.URL(proxy).host if proxy else ""
    if not proxy or "refresh" not in host:
        detail = f"host {host!r}" if proxy else "unset"
        raise SystemExit(
            f"AUDIBLE_PROXY_URL ({detail}) does not name a "
            f"refresh-dedicated exit. Refusing to raise concurrency to "
            f"{CONCURRENCY_MAX} against what may be the shared production "
            "proxy -- point this at the dedicated refresh exit before starting."
        )


def _verify_backlog_headroom() -> None:
    """
    Refuses to start when BACKLOG_HIGH_WATER leaves no room to ever admit a
    chunk -- checked against the actual resolved constant, not just its
    default derivation, so an explicit REFRESH_BACKLOG_HIGH_WATER override is
    judged on what it actually resolves to rather than assumed bad or good.

    _env_int applies no floor to REFRESH_CONCURRENCY_MAX, and the module
    docstring explicitly invites raising it "if the endpoint is visibly
    bored," citing 250 as measured clean elsewhere. At the default derivation,
    CONCURRENCY_MAX = 100 already consumes the whole 5000-book
    backlog_capacity() in reserve, leaving a high water of zero; above that it
    goes negative. _wait_for_backlog then loops on `queued_books() >
    BACKLOG_HIGH_WATER`, which is true even at a queue depth of zero -- the
    dispatcher logs one line, hands out no chunks, and sits until SIGTERM,
    against a rented dedicated exit, on a run whose whole point is to finish
    unattended overnight.

    Refuses rather than clamping with max(...): a clamp would leave the guard
    present but silently useless -- the operator raises the knob, sees no
    error, and gets a run with no backpressure at all instead of a run that
    is merely slower to open up. Requires at least one full chunk of
    headroom, not merely a positive water mark, so the dispatcher always has
    room to admit the next chunk rather than immediately blocking on one it
    just let through.
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
    Widens the two application limits that would otherwise cap this run below
    anything the ramp could reach — in this process only, before anything has
    built a client, a semaphore or a session.

    Both are sized for the API process and both are correct there. Neither
    constraint exists in a dedicated one-off container:
    - AUDIBLE_CONCURRENCY_LIMIT is a slice of what a SHARED exit IP tolerates
      and, separately, the fan-out width of one live request. This container
      has an exit to itself and serves no requests, so the ramp is the only
      thing that should be deciding the width.
    - The shared client's connection pool is sized off that same limit, so
      leaving it alone would queue the ramp's requests behind 35 connections
      no matter what the semaphore allowed.
    - Background persist concurrency is 2 so the seeder cannot starve the
      API's connection pool. Nothing shares this pool, and at 2 the write side
      becomes the run's ceiling well before Audible does.

    Rebinding module attributes is deliberate and is confined to this one
    function. The alternative is making three application constants
    environment-driven for the sake of a script that gets deleted after one
    night, which is a permanent change to production code paying for a
    temporary need. The asserts are the safety: both application objects are
    built lazily on first use, so this is only correct before first use, and a
    later caller would silently get the old limits instead.
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
