"""
One-off chapter backfill.

Walks books that haven't had their chapters fetched yet (chapters_checked_at IS
NULL), pulls each book's chapters from Audible, and stores them -- gently, over
a long stretch, so it doesn't look like a scraper and doesn't get the exit IP
flagged.

Design notes:
- Routes Audible calls through the AUDIBLE_PROXY_URL set for THIS process, a
  dedicated VPN exit separate from the live app's, so if this IP gets throttled
  the live service is unaffected. Not a convention but a precondition: the run
  refuses to start unless the hostname names a backfill exit -- see
  _verify_dedicated_proxy.
- Reuses the app's own fetch/normalize/write path (audible_get, the chapter
  normalizer, and an upsert of the same Track row) so stored data is identical
  to on-demand fetches.
- The work queue is self-checkpointing: a book leaves it the moment
  chapters_checked_at is set, whether we stored chapters, found none, or Audible
  404s the record.
- A 404 (NotFoundException) is terminal: some records -- notably ISBN-keyed ones
  (~7% of the catalog, non-B ASINs) -- return no chapter metadata in ANY region,
  confirmed by cross-region probing. Retrying them would waste requests, never
  drain the queue, and falsely trip the error back-off. So a 404 is marked
  checked and counted as "not found", NOT as an error.
- A confirmed-permanent non-404 status is terminal too, the same way: currently
  just 400, since that's the only one actually observed -- the same ~43 ASINs
  answered 400 on every cycle across six days of production logs, the set
  growing monotonically (2 to 43) and never clearing on retry. client.py's
  AudibleAPIException carries the real upstream HTTP status
  (`upstream_status`; `None` for a timeout or connection failure, which never
  reached Audible at all), so this script marks a permanent status checked and
  counts it as "permanent" rather than looping it forever as a generic error.

CONCURRENCY. Single-threading was a historical artefact, not a stealth choice --
concurrency didn't exist when this script was first written. A resizable gate
(_Gate, ported from refresh_corpus.py) now admits up to CONCURRENCY_CEILING (10)
books in flight, climbing from CONCURRENCY_START (3) on evidence via _Ramp, with
a per-slot desynchronising jitter after each release. The ceiling is fixed at
10 -- never AUDIBLE_CONCURRENCY_LIMIT's 48-wide cousin in refresh_corpus -- for
two reasons detailed in the constants block below and asserted in code: the
evidence justifying 48 there measured byte rate, not request rate, and a gate
past AUDIBLE_CONCURRENCY_LIMIT would have the ramp misread its own semaphore
queuing as exit degradation. See _Gate, _Ramp and the CONCURRENCY_* /
RAMP_MIN_* / SLOT_JITTER_* constants for the full reasoning behind each number.

RATCHET, NOT ABORT-ON-FIRST-429 (see _Ratchet). refresh_corpus aborts on its
first 429 because it has nowhere to retreat to; this script does -- concurrency-
1 is its own proven-safe shipped behaviour and every book commits for itself,
so retreat costs nothing. A 429 (raised, or absorbed by audible_get's internal
retry and visible only via _ThrottleSentinel) or a 401/403, the FIRST time,
drops to the floor, freezes the ramp, restores the historical delay, cools
down, and re-logs the exit IP. A SECOND such signal, ever again, aborts. 401/403
is promoted to this tier because an anonymous 401/403 is far more likely a real
IP/proxy block than a per-ASIN fact (live-checked against the real API -- see
_is_backoff_signal); one vote in a fifty-sample window under-weighted that.

THE FALSE-NONE GUARD (_NoneRateGuard). _process_one marks a book checked on ANY
200 lacking content_metadata.chapter_info, and nothing ever revisits it --
seeder.py skips chapters_checked_at IS NOT NULL for good. A degenerate 200 (a
soft-block interstitial, an error envelope served as 200, a dropped response
group) would otherwise drain the remaining queue at full concurrency, silently
mislabelling every book "has no chapters", landing between two progress lines
at this pace. The guard compares a rolling window's NONE share against the
run's own settled baseline and aborts on a clear spike -- with no assumption
about a genuine 200's shape; see _NoneRateGuard for why a structural canary was
considered and deliberately not built.

DATABASE. Every concurrent unit gets its OWN AsyncSession -- see _dispatch_one.
The walk is keyset-paginated on `asin`, exactly as refresh_corpus._read_page,
replacing the old `WHERE chapters_checked_at IS NULL` query that re-walked the
whole stamped prefix on every batch (no index on that column) -- quadratic,
hidden by the old 1s sleep, exposed once concurrency raises the request rate.
See _read_page and _advance_cursor for the paging itself, the two accepted
consequences of dropping the IS NULL filter, and the one-shot wrap-around pass.

Rate, once past the ratchet's floor: N concurrent slots (climbing 3 -> 10) each
pacing themselves with a desynchronising jitter, plus the original macro on/off
schedule (~12h active, then a random pause) -- traffic still isn't mechanically
uniform even at full width.

Run as a separate container off the Libex image:

    docker run -d --name libex-chapter-backfill \\
      --network libex-proxy \\
      -e AUDIBLE_PROXY_URL=http://libex-backfill-vpn:8888 \\
      -e DATABASE_URL=<same as the app> \\
      ghcr.io/libexhq/libex:latest \\
      python -m scripts.backfill_chapters --limit 5     # dry run; drop --limit for the real run

Stop with `docker stop libex-chapter-backfill` -- it finishes whatever books are
currently in flight, commits them, and exits cleanly.

ENVIRONMENT. Everything this container reads. The first two are required and
the run dies without either, though differently -- see each. The rest have
working defaults, env-overridable so a live run can be adjusted without a
rebuild -- except the concurrency ramp, the ratchet, and the NONE-rate guard,
which are deliberately plain constants with no env override at all -- a
knob for every value is a decision left unmade in public; see the CONCURRENCY,
RATCHET and THE FALSE-NONE GUARD sections above for those numbers and why.

    DATABASE_URL               the same database the app uses, read straight
                               from the environment -- unset is a KeyError on
                               startup, not a fallback. The image entrypoint
                               also runs `alembic upgrade head` against it
                               before this script gets control.
    AUDIBLE_PROXY_URL          the dedicated VPN exit this run leaves by. Its
                               hostname must contain "backfill" --
                               _verify_dedicated_proxy refuses to start on
                               anything else, because an unset value sends
                               every request out by the container's own egress,
                               the host's public address, and a value copied
                               from another stack points this run at whatever
                               exit that stack uses -- and if that is the live
                               service's, a weeks-long crawl would land on the
                               exit the service depends on, which is what this
                               check exists to stop. It tests that substring and
                               nothing else, so a genuinely dedicated exit named
                               without the word is refused exactly as a shared
                               one is.

    LOG_LEVEL                  INFO    DEBUG, INFO, WARNING or ERROR. WARNING and
                                       above drop the RESUME CURSOR line a
                                       restart depends on, which is logged at
                                       INFO. ERROR additionally kills the
                                       sentinel a 429 depends on entirely: the
                                       throttle record it watches is emitted at
                                       WARNING by audible_get's own retry
                                       branch (client.py), and a logger
                                       that never creates a WARNING record
                                       leaves _ThrottleSentinel permanently
                                       blind. An absorbed 429 is only ever
                                       visible there -- _process_one
                                       deliberately doesn't report a raised one
                                       either, since the sentinel already has
                                       it covered -- so at ERROR the ratchet
                                       never trips on a 429, and the sustained-
                                       5xx abort dies with it too, the same
                                       detector serving both (see RATCHET).
                                       401/403 stays covered regardless -- that
                                       path is classified directly in
                                       _process_one, not through logging.
                                       DEBUG=true forces DEBUG whatever this
                                       says.
    AXIOM_TOKEN                (unset) set means every line of this run ships to
                                       Axiom as well as stdout. Copying another
                                       stack's environment is the easy way to
                                       inherit it without meaning to.
    AXIOM_DATASET              libex   the dataset those lines land in.
    LOG_RETENTION_DAYS         7       rotation of the log file inside the
                                       container; 0 keeps everything. The handler
                                       is attached whether or not a logs volume
                                       is mounted, so 0 across a weeks-long run
                                       grows the file in the container layer
                                       unbounded.

    BACKFILL_DELAY_MIN         0.7     per-request delay floor, seconds -- only
                                       applies once the ratchet has dropped the
                                       run to CONCURRENCY_FLOOR; see RATCHET.
    BACKFILL_DELAY_MAX         2.0     per-request delay ceiling at the floor,
                                       seconds.
    BACKFILL_ACTIVE_HOURS      12.0    hours awake before a pause
    BACKFILL_PAUSE_MIN_HOURS   4.0     shortest pause between active stretches
    BACKFILL_PAUSE_MAX_HOURS   11.0    longest pause between active stretches
    BACKFILL_CHUNK_SIZE        500     keyset page size for the corpus walk
                                       (see _read_page), not a rate knob.
    BACKFILL_ERROR_WINDOW_SECONDS
                               65.0    rolling TIME window, seconds, for the
                                       general soft back-off signal rate (a
                                       timeout or a 5xx -- 429/401/403 are the
                                       ratchet's, not this window's).
    BACKFILL_ERROR_THRESHOLD_RATE
                               0.5     signal share within that window that
                                       trips cooldown.
    BACKFILL_ERROR_MIN_SAMPLES 10      samples required in the window before
                                       the rate above may trip anything.
    BACKFILL_ERROR_COOLDOWN    1800.0  cooldown length, seconds -- also what
                                       the ratchet's own floor-trip pause uses.
    BACKFILL_PROGRESS_EVERY    50      books between progress lines

Lowering the delays or raising the active hours is what makes the run look
less organic, which is the one thing this script is shaped to avoid.
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
# _Ratchet): a random draw in [MIN, MAX] seconds -- exactly the single-
# threaded pacing this script shipped with before concurrency existed.
DELAY_MIN = _env_float("BACKFILL_DELAY_MIN", 0.7)
DELAY_MAX = _env_float("BACKFILL_DELAY_MAX", 2.0)

# per-slot jitter while running above the floor -- desynchronisation, not rate
# control; see the module docstring's CONCURRENCY section for why it's shorter
# than DELAY_MIN/MAX. Plain constants: this would be a brand-new knob, and a
# knob nobody has yet had a reason to turn is a decision left unmade, so it
# isn't env-tunable the way the historical delay is.
SLOT_JITTER_MIN = 0.25
SLOT_JITTER_MAX = 0.75

# macro on/off: run for ACTIVE_HOURS, then pause a random number of hours in
# [PAUSE_MIN, PAUSE_MAX], then resume. Breaks up the daily traffic signature.
ACTIVE_HOURS = _env_float("BACKFILL_ACTIVE_HOURS", 12.0)
PAUSE_MIN_HOURS = _env_float("BACKFILL_PAUSE_MIN_HOURS", 4.0)
PAUSE_MAX_HOURS = _env_float("BACKFILL_PAUSE_MAX_HOURS", 11.0)

# --- concurrency ramp --------------------------------------------------------
#
# Plain module constants, not env-tunable -- see the module docstring's
# CONCURRENCY section for both reasons the ceiling is fixed at 10.
CONCURRENCY_FLOOR = 1
CONCURRENCY_START = 3
CONCURRENCY_STEP = 3
CONCURRENCY_CEILING = 10

# Never wider than the API's own fan-out width -- asserted, not just
# commented, so this can't silently regress if either constant moves.
assert CONCURRENCY_CEILING <= audible_client.AUDIBLE_CONCURRENCY_LIMIT, (
    f"CONCURRENCY_CEILING ({CONCURRENCY_CEILING}) must not exceed "
    f"AUDIBLE_CONCURRENCY_LIMIT ({audible_client.AUDIBLE_CONCURRENCY_LIMIT}) -- "
    "see the module docstring's CONCURRENCY section."
)

# Per-rung dwell: BOTH floors must be met before the next step up -- see the
# module docstring's CONCURRENCY section for why the time floor is load-
# bearing and the request floor alone (refresh_corpus's RAMP_INTERVAL) is not.
RAMP_MIN_SECONDS = 60.0
RAMP_MIN_REQUESTS = 300

# Degradation, ported from refresh_corpus: latency sampled per Audible
# request, kept separately per region, a level's p95 compared only against
# that SAME region's own best -- never pooled. Past this ratio, the run steps
# down one rung and freezes further climbing for the rest of the run. Three
# warmup windows before the check may act at all -- refresh_corpus measured
# its own ramp freezing at the opening rung on nothing but first-window
# jitter (1104ms against a 469ms best, 1.3 minutes in); the warmup lets
# best_p95 settle before anything gets judged against it.
LATENCY_WINDOW = 60
DEGRADE_P95_RATIO = 2.0
DEGRADE_WARMUP_SAMPLES = LATENCY_WINDOW * 3

# General back-off: a timeout/connection failure or a 5xx (see
# _is_backoff_signal). Re-scoped from count-based to TIME-based: the original
# ERROR_WINDOW = 50 spanned ~65s at the historical ~0.77 req/s pace, but only
# ~4s at the 12 req/s this ceiling now produces -- a window whose real span
# changes with concurrency isn't a stable window. ERROR_WINDOW_SECONDS keeps
# ~65s of evidence at any pace; ERROR_THRESHOLD_RATE (a rate, not a raw
# count) stays comparable to the original 25/50 = 50% design. 429/401/403 are
# excluded now -- see _is_backoff_signal and _Ratchet for the stricter
# mechanism those get instead of a vote in this softer window.
ERROR_WINDOW_SECONDS = _env_float("BACKFILL_ERROR_WINDOW_SECONDS", 65.0)
ERROR_THRESHOLD_RATE = _env_float("BACKFILL_ERROR_THRESHOLD_RATE", 0.5)
ERROR_MIN_SAMPLES = _env_int("BACKFILL_ERROR_MIN_SAMPLES", 10)
ERROR_COOLDOWN = _env_float("BACKFILL_ERROR_COOLDOWN", 1800.0)  # 30 min -- also the ratchet's own cooldown

# Sustained 5xx, ported from refresh_corpus's own _ThrottleSentinel: a handful
# in a long run is unremarkable, a sustained run means real trouble. Plain
# constants -- new to this file, not a renamed pre-existing knob.
ABORT_5XX_WITHIN = 20
ABORT_5XX_WINDOW_SECONDS = 120.0

# Rolling NONE-rate abort -- see _NoneRateGuard for the reasoning behind the
# mechanism and these numbers: NONE_RATE_BASELINE_MIN_SAMPLES is the settled
# history required BEHIND the recent window before a comparison is trusted;
# NONE_RATE_ABSOLUTE_FLOOR keeps a catalog segment that's just genuinely
# NONE-heavy from tripping this alone; NONE_RATE_SPIKE_MULTIPLIER is how far
# past baseline counts as a spike rather than ordinary variation. Both are
# reasoned heuristics -- no historical NONE-rate measurement exists to
# calibrate against more precisely.
NONE_RATE_WINDOW = 60
NONE_RATE_BASELINE_MIN_SAMPLES = 200
NONE_RATE_ABSOLUTE_FLOOR = 0.5
NONE_RATE_SPIKE_MULTIPLIER = 3.0

# keyset page size for the corpus walk (see _read_page) -- memory/round-trip
# scope only, not a rate knob: every row on a page is a database read, most of
# them already checked and immediately discarded, and only the ones still
# NULL ever reach Audible.
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
    Best-effort hostname for a log line -- never the raw AUDIBLE_PROXY_URL.

    httpx's proxy= accepts credentials embedded in the URL
    (http://user:pass@host:port), and Settings stores the value as a plain
    str, not a SecretStr, so nothing that reaches a log record may be the
    full value. The hostname is the diagnostically useful part -- which exit
    this run is using -- and carries no secret, so that's what gets logged
    instead. Must never raise: a logging call taking the whole run down over
    a malformed env var would turn a cosmetic problem into an outage.
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
    Refuses to start unless AUDIBLE_PROXY_URL both is set and names this
    run's own exit.

    Without this, an unset value reaches httpx.AsyncClient as proxy=None and
    this script's Audible traffic egresses DIRECT FROM THE CONTAINER -- the
    same public address the live service answers on. Today that's a slow
    trickle; it's also the one path from "backfill trouble" to "Libex is
    down", so the run must die here rather than discover it request by
    request.

    Checked against the hostname, not a literal URL, because the real
    production proxy value is infrastructure this app never carries in
    source and has no secret to compare against. "backfill" in the hostname
    is the convention this script's own module docstring and RUN IT example
    already commit to (libex-backfill-vpn) -- the same convention
    scripts/refresh_corpus.py's own _verify_dedicated_proxy cites as shared
    between the two scripts. An operator who leaves the variable unset, or
    reuses the shared/live value, fails this on hostname alone, before a
    single request goes out.

    The failure message names the hostname only, never the full value:
    httpx's proxy= accepts credentials embedded in the URL
    (http://user:pass@host:port) and nothing in Settings forbids
    AUDIBLE_PROXY_URL being configured that way, so the one path that fires
    on operator misconfiguration must not be the one that echoes back
    whatever the operator typed, including a possible credential.

    Logged, not just raised: SystemExit propagates straight out of the
    process without ever touching the libex logger, so on its own it would
    survive only as stderr text -- in a container that runs unattended, the
    highest-severity startup condition this script has would be the one
    piece of evidence that never reaches the rotating file handler or
    Axiom. The log call is made first, with the same hostname-only
    discipline as the SystemExit message, so the raw value can't reach it
    either.

    Hostname extraction goes through _proxy_host_for_log rather than a bare
    httpx.URL(proxy).host, deliberately: a value that is set but malformed
    (a typo'd port is the realistic case -- a Portainer env field is free
    text) makes httpx.URL raise InvalidURL, and that must fail exactly like
    an unset or wrongly-named value -- logged and refused -- not escape as
    an uncaught traceback that skips both the log line and the deliberate
    SystemExit message. _proxy_host_for_log already turns that same
    exception into "(unparseable)", which reads fine as a proxy_host value
    and correctly fails the "backfill" in host check below, so there is no
    second try/except to keep in sync with the first.
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
    """One-time startup check: confirm which IP our proxy actually exits from.

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
    A concurrency gate whose limit moves while tasks are waiting on it.

    Ported from scripts/refresh_corpus.py rather than reinvented:
    asyncio.Semaphore cannot shrink -- releasing extra permits grows it, but
    nothing takes permits back -- and the ratchet needs to step this down to
    CONCURRENCY_FLOOR as readily as the ramp steps it up. A condition
    variable over an active count does both, and a step down takes effect as
    tasks finish rather than by cancelling work already in flight.
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
        wall-clock time since the current clean streak began -- see the
        module docstring's CONCURRENCY section for why the count alone
        (refresh_corpus's ported RAMP_INTERVAL) isn't enough here."""
        return (
            self.clean_streak >= RAMP_MIN_REQUESTS
            and self.streak_started is not None
            and time.monotonic() - self.streak_started >= RAMP_MIN_SECONDS
        )


class _Ramp:
    """
    Climbs the shared gate on evidence from every region and steps it down on
    degradation in any one -- ported from refresh_corpus._Ramp; see that
    class's own docstring for the full reasoning behind per-region tracking
    against a global gate. The one behavioural difference is the step-up
    condition: dwell_met() (time AND count) rather than a bare clean-streak
    count, and the ceiling/step size are CONCURRENCY_CEILING/CONCURRENCY_STEP.
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
        ramp's own ordinary degrade/step-up machinery permanently inert on
        top of the ratchet's decision, so nothing here fights it."""
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
        # Reset before the await, not after, mirroring _step_down's own
        # self._frozen = True as its first statement -- ported unchanged
        # from refresh_corpus._Ramp._step_up, which is where this was
        # reproduced and is worth reading in full: set_limit acquires the
        # gate's shared condition variable, the same lock every concurrent
        # unit contends on through gate.acquire()/release(), so this await
        # genuinely suspends under load. A concurrent record() that runs
        # while it's suspended must not still see every region as clean, or
        # it independently re-satisfies the same "all warm regions clean"
        # check and calls _step_up() again before this climb has even
        # landed, stacking several climbs onto what should have been one
        # (reproduced there: 30 concurrent record() calls against a
        # contended gate produced 30 _step_up() calls instead of 1, and
        # reproduced again here at this file's own scale -- 180 concurrent
        # record() calls all satisfying dwell at once still stepped the
        # gate exactly one rung). Nothing yields between
        # reading the pre-reset state and applying it here, so no other
        # decision can act on a stale streak -- the same guarantee
        # self._frozen already gives _step_down.
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
    Watches for the throttle line audible_get emits on every 429 and 5xx it
    receives, including the ones a retry then absorbed.

    An absorbed 429 never becomes an exception, so this is the only place it
    is visible -- and an absorbed 429 is precisely the early warning the
    ratchet exists to act on. It keys on the structured fields rather than
    the message text: status_code together with attempts_left is emitted by
    exactly one call site in the codebase (the retry branch of audible_get),
    which makes the coupling a field contract rather than a string match.

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


# --- the ratchet: first 429/401/403 to the floor, second aborts ------------

class _Ratchet:
    """
    First 429 (raised or sentinel-absorbed) or 401/403, ever: drop to
    CONCURRENCY_FLOOR, freeze the ramp, restore the historical delay, cool
    down, re-log the exit IP. Any FURTHER such signal after that: abort.

    Deliberately not refresh_corpus's abort-on-first-429: that script has
    nowhere to retreat to. This one does -- concurrency-1 is its own proven-
    safe shipped behaviour, and every book commits for itself, so retreating
    costs nothing.

    observe() takes each book's own auth_trouble flag (True only for a raised
    401/403 -- see _process_one for why 429 never sets it) plus the
    sentinel's running total, folding in the DELTA of new 429 sightings since
    the last call -- a delta because the sentinel's counter never resets and
    several concurrently-dispatched books can each observe a jump between two
    calls. Every new event in a call is processed, not just the first, so two
    signals landing in the same beat still ratchet through both stages.
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
    Rolling TIME-based window of recent soft-backoff signals (a timeout,
    connection failure, or 5xx -- see _is_backoff_signal). Re-scoped from
    count-based because a fixed request count is a different amount of
    wall-clock evidence at every concurrency this run now moves through --
    see the comment above ERROR_WINDOW_SECONDS for the measured numbers.
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
    this run's own established baseline -- see the module docstring's THE
    FALSE-NONE GUARD section for the blast-radius reasoning this exists for.

    The baseline is computed from history STRICTLY BEFORE the recent window
    (total minus the window, not the running total including it), so a
    spike inside the window can't drag its own comparison baseline up while
    it's happening.
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
    primary key -- exactly refresh_corpus._read_page's own pattern.

    Filtering to chapters_checked_at IS NULL happens after this call, in
    Python, not in this WHERE clause: chapters_checked_at has no index, so a
    filtered query forces Postgres to heap-fetch every row of the growing
    stamped prefix on every single page, on top of the index range scan
    `asin > cursor` already gives for free -- the quadratic cost this whole
    change exists to remove. Walking every row once, checked or not, and
    discarding the checked ones in-process costs one cheap conditional per
    row instead.
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
    Pure wrap-around policy, pulled out of the run loop so it's directly
    testable without a database: reached the end of the corpus once -> wrap
    the cursor back to the start for one more pass (catches a book the
    seeder inserted below where this run started); reached the end a SECOND
    time, after already having wrapped -> genuinely done.

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
# record rather than a failure worth retrying. Limited to 400 -- see the
# module docstring above for the evidence (six days of production logs, the
# ~43-ASIN set, and the live-confirmed "non_audio asset with
# contentDeliveryType:Bundle" message). 410 would belong here too if Audible
# ever returned it for this endpoint, but there's zero evidence of that, so
# it isn't added speculatively. 401/403 are deliberately NOT here -- see
# _is_backoff_signal, _Ratchet, and the module docstring for why they're kept
# retryable and promoted to the ratchet instead.
_PERMANENT_UPSTREAM_STATUSES = {400}


def _is_backoff_signal(upstream_status: int | None) -> bool:
    """True only for a timeout/connection failure (upstream_status is None)
    or a 5xx -- the general soft trouble the time-based _BackoffWindow still
    covers. 401/403 and 429 are deliberately excluded now: both are promoted
    to _Ratchet instead, a stricter floor-and-freeze mechanism a single vote
    in a rolling window can't express -- see the module docstring's RATCHET
    section. Excluded the same way as before: confirmed per-record facts
    (404, and any status in _PERMANENT_UPSTREAM_STATUSES) never reach this
    function at all, since _process_one returns before calling it for those
    cases."""
    if upstream_status is None:
        return True
    return 500 <= upstream_status < 600


async def _process_one(
    session: AsyncSession, asin: str, region: str
) -> tuple[str, bool, bool, float]:
    """
    Fetches and stores one book's chapters.

    Outcomes:
    - STORED / NONE / NOT_FOUND / PERMANENT: the book is marked checked and
      leaves the queue. These mean, respectively: chapters saved; resolved
      with none; a 404; a confirmed-permanent non-404 status (see
      _PERMANENT_UPSTREAM_STATUSES). None of these count as an error.
    - ERROR: an unresolved failure. Left unmarked so a later pass retries
      it, and always counts toward the plain error total.

    Returns (outcome, is_backoff_signal, is_ratchet_signal, elapsed).
    Both signal flags are only ever True alongside ERROR: is_backoff_signal
    for a timeout or 5xx (see _is_backoff_signal), is_ratchet_signal ONLY for
    a raised 401/403. A 429 never sets is_ratchet_signal here -- an absorbed
    one never reaches this function as an exception, and even a fully-
    exhausted one that DOES raise has already been logged by audible_get's
    own retry branch (every attempt, including the last), so _ThrottleSentinel
    has already seen it; reporting it again would double-count the event
    against _Ratchet. elapsed is the audible_get call's own duration, always
    measured on every path, so the ramp's latency signal is never contaminated
    by the DB write that follows it for a STORED/NONE outcome.
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
        # write failure -- leave unmarked so we retry. Not a signal either:
        # a local write failure says nothing about the exit IP.
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

    run is optional (several call sites run before a _Run exists yet) but
    should be passed whenever one is in scope: without it, a long sleep --
    the ratchet's or the soft window's 30-minute ERROR_COOLDOWN, most of
    all -- only watches stopper.stopping (SIGTERM/SIGINT) and would sleep
    out its full length even if run.abort_reason gets set concurrently by
    another in-flight unit during that same wait.
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
    One concurrent unit of work. The caller has already acquired a gate
    permit (see _run's dispatch loop) -- this releases it, then sleeps this
    slot's own jittered delay AFTER releasing, never while holding it, so
    the delay never occupies a concurrency slot doing nothing. Every unit
    gets its own AsyncSession: AsyncSession is not concurrency-safe, and a
    shared one's rollback path would roll back another unit's committed-
    looking work.

    Every escape from the block below -- opening the session, or anything
    _process_one itself doesn't already catch -- becomes a counted ERROR
    outcome rather than an exception that keeps propagating. _run collects
    these tasks with asyncio.gather(..., return_exceptions=True) and never
    inspects the result, so before this catch an exception here vanished
    silently: no log line, no counter, the cursor still advancing past the
    page regardless (see _advance_cursor), and the run finishing with exit
    code 0. Pre-slice, _process_one was awaited directly in the run loop and
    the same exception killed the process with a traceback -- moving it
    into a task under return_exceptions=True is what made this possible, so
    it's this slice's own regression to close. Mirrors the shape
    scripts/refresh_corpus.py's own _refresh_chunk already uses for the same
    reason, in its `except Exception as exc:` branch -- named rather than
    line-cited, since a docstring-only edit to that file has already once
    shifted the number this comment used to carry.
    """
    outcome, is_backoff_signal, is_ratchet_signal, elapsed = _Outcome.ERROR, False, False, None
    try:
        try:
            async with Session() as session:
                outcome, is_backoff_signal, is_ratchet_signal, elapsed = await _process_one(
                    session, asin, region
                )
        except Exception as exc:
            # Reachable in practice, not just in theory: _mark_checked runs
            # unguarded on the NOT_FOUND/PERMANENT/NONE paths inside
            # _process_one, and this slice's own statement_timeout (see
            # _run's create_async_engine call) raises on exactly those calls
            # against a stricken database. Feeds the ramp (below, via
            # ramp_failed) and the general soft back-off window (True) --
            # unlike _process_one's own internal "the response body didn't
            # parse" case, which stays excluded from the window on purpose,
            # anything reaching all the way out here escaped _process_one's
            # careful per-status classification entirely, including a
            # session/connection failure against a now-timeout-bounded
            # database, which really can mean the whole run is under
            # strain and pausing is the right call. Does NOT feed the
            # ratchet (False): the ratchet exists for evidence about the
            # EXIT specifically (a 429, a 401/403), and a DB-side failure is
            # no such evidence -- tripping the ratchet on it would drop to
            # the floor, and eventually abort, over database trouble that
            # has nothing to do with Audible or the exit IP.
            logger.warning("Backfill: unit failed", extra={
                "asin": asin,
                "region": region,
                "error_type": type(exc).__name__,
                "sqlstate": getattr(getattr(exc, "orig", None), "sqlstate", None),
            })
            outcome, is_backoff_signal, is_ratchet_signal, elapsed = _Outcome.ERROR, True, False, None
    finally:
        await gate.release()

    # This block of shared-state bookkeeping (counters, ramp, backoff
    # window, ratchet, none-guard) is atomic per completed task under
    # cooperative scheduling EXCEPT across its two real suspension points:
    # `await ramp.record(...)` below, when it reaches a ramp transition
    # (_step_up/_step_down), and `await gate.set_limit(CONCURRENCY_FLOOR)`
    # in the ratchet's own floor branch a few lines further down. Both
    # acquire the gate's shared asyncio.Condition -- the same contended
    # lock _Gate's own docstring and _step_up's ported comment already
    # describe as genuinely suspending under load (measured: holding that
    # condition, a concurrent set_limit() did not complete across five
    # event-loop turns). So a concurrent task's own bookkeeping CAN interleave
    # with this one's at either point. The only place that matters: the
    # `run.processed % PROGRESS_EVERY` check further down reads this same
    # task's own increment from a few lines up, so a progress line can be
    # skipped (never duplicated or corrupted) if another task's own check
    # lands in that window instead. Everything else here -- the counter
    # increments, backoff_window.record(), ratchet.observe(),
    # none_guard.record(), the sentinel checks -- is synchronous and either
    # order-insensitive or internally atomic on its own.
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
    tripped since the last check -- draining whatever's currently in
    flight first, so nothing keeps calling Audible during the pause.

    Call this from every point in _run's dispatch loop where a just-
    completed task could have set either flag, not only from inside the
    per-task loop body: a flag set by the LAST task dispatched on a page
    has no "next iteration" of that loop to be noticed from, and a check
    that lived only in the loop body silently skipped the ratchet's
    30-minute cooldown and exit-IP relog on exactly that trip -- worse the
    further a long run gets, since later pages thin out to a handful of
    rows apiece and a trip landing on the last one stops being a corner
    case. _run calls this once per dispatched task AND once more right
    after the page's own post-loop drain, so a flag set during that drain
    is still caught before the next page is ever read. When called after
    that drain, inflight is already empty and the "drain first" step below
    is simply a no-op -- safe to call unconditionally either way.
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
    # 5xx, or a NONE-rate spike. Distinct from 0 so a supervisor can tell
    # "stopped for its own protection" apart from an ordinary shutdown --
    # the same non-zero-on-abort convention refresh_corpus uses, though this
    # script has no code-2 case: every write is a per-book commit inside
    # _process_one itself, so there's no background queue that can finish
    # the run non-empty the way refresh_corpus's persist queue can.
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
