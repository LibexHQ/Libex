"""
Standalone seeder entry point.

app/main.py's FastAPI lifespan used to start run_seeder() and
run_new_releases_seeder() as background tasks, but lifespan runs once PER
WORKER PROCESS and WEB_CONCURRENCY runs six of them. Neither coroutine
coordinates across processes -- each opens with the same
`await asyncio.sleep(30)`, so all six workers wake within milliseconds of each
other and run byte-identical `last_seeded_at IS NULL OR < cutoff` selections
before any of them has stamped a single row: the stamp only lands after the
FULL Audible walk for that entity, minutes to hours later, so it dedupes
nothing between the six. SEEDER_REQUEST_DELAY paces one process's own
requests; it says nothing about what five siblings are doing at the same
moment, so the delay Audible actually sees between requests is one sixth of
what is configured, and the request volume landing on one exit IP is six
times what a single run would produce. This script exists so the seeder runs
as ONE process, in its own container, on its own exit -- exactly once, not
once per worker.

Running this container IS the enable decision. There is no separate flag:
either the container is running or the seeder is not, so a leftover
SEEDER_ENABLED in the environment (retired alongside this move) has no effect
either way -- checking it here would let the container start, both coroutines
return immediately, and the process exit 0 having done nothing, with nothing
in the log to say why.

WHAT THIS SUPPLIES THAT THE LIFESPAN GAVE FOR FREE.
- Log handlers: get_logger() alone attaches none, so main() calls
  setup_logging() before anything else, the same as every other standalone
  script in this directory.
- Proxy containment: _verify_dedicated_proxy(), mirroring the "backfill" and
  "refresh" checks scripts/backfill_chapters.py and scripts/refresh_corpus.py
  already make -- refuses to start unless AUDIBLE_PROXY_URL names a
  seeder-dedicated exit, so this run's traffic can never land on the shared
  exit the live service depends on.
- Clean shutdown: see _Stopper below. Without a SIGTERM handler, `docker stop`
  hard-kills mid-request and the persist queue this script drains (see next)
  never gets the chance to.
- --once: a single supervised cycle of each worker rather than the forever
  loop, threaded through as a plain parameter on run_seeder and
  run_new_releases_seeder (app/services/seeder.py) rather than built here, so
  the same behavior is available to anything else that imports those
  coroutines directly.

WHAT ACTUALLY STOPS THE LOOPS. Both run_seeder and run_new_releases_seeder are
a bare `while True`, with no stop flag of their own -- the lifespan tore them
down with asyncio.Task.cancel() on shutdown, and nothing else ever unwinds
them. _Stopper mirrors that exactly: SIGTERM/SIGINT cancels both tasks
directly rather than setting a flag they poll, since neither loop polls one.
Every unit of work inside them is already its own bounded transaction --
each phase in app/services/seeder.py opens `async with SessionFactory() as
session:` per author, per series, per chunk of books, and commits before the
next `await` that could be cancelled -- so a cancellation lands between two
already-committed units, never inside one: the session's own `__aexit__`
rolls back whatever that one unit hadn't committed yet, and Postgres never
sees a transaction left dangling on a connection nothing is driving anymore.

WHAT CANCELLATION DOES NOT COVER, AND WHY THIS DRAINS THE PERSIST QUEUE.
get_books_by_asins and fetch_author_books_by_name -- the two calls that do
almost all of this script's writing -- persist through
app/services/db/persist_queue.py's fire-and-forget background tasks
(persist_books_background, persist_author_background, and so on): the
coroutine that queues a write returns as soon as the task is SPAWNED, not
once it lands, and _spawn's task is a sibling of run_seeder's own task, not a
child it awaits. Cancelling run_seeder therefore does nothing to a write
already in flight in one of those sibling tasks, and asyncio.run() abandons
any task still pending when the coroutine it was given returns -- silently,
with no exception and no log line, which is exactly the "chunk half-persisted"
failure mode the brief for this script named as the thing to rule out. Ruled
out here by draining: after both workers are cancelled (or, under --once,
finish on their own), this script waits on persist_queue.queued_books() to
reach zero, bounded by DRAIN_TIMEOUT_SECONDS, before it disposes the engine
and exits. fetch_and_store_chapters (the seeder's chapter-gathering path,
called from _gather_chapters) needs none of this: it writes through its own
session directly, INSIDE the coroutine that calls it, with its own commit --
never through persist_queue -- so it depends on nothing this script or the
lifespan ever supplied, and a cancellation there is covered by the same
per-unit commit boundary as everything else in the loops.

RUN IT (its own container, its own dedicated exit -- AUDIBLE_PROXY_URL must
name it explicitly; the run refuses to start unless its hostname contains
"seeder", see _verify_dedicated_proxy):

    docker run -d --name libex-seeder \\
      --network libex-proxy \\
      --network libex_default \\
      -e DATABASE_URL=<same as the app> \\
      -e AUDIBLE_PROXY_URL=http://libex-seeder-vpn:8888 \\
      ghcr.io/libexhq/libex:latest \\
      python -m scripts.seed --once     # one supervised cycle; drop --once to run forever

Both networks are needed: libex-proxy reaches the VPN sidecar, and the app
stack's own network is the only place the `postgres` host in DATABASE_URL
resolves -- the same two-network requirement scripts/refresh_corpus.py's own
RUN IT section documents in full, including how to find the stack network's
real name when it isn't literally `libex_default`.

Stop with `docker stop libex-seeder` -- SIGTERM cancels both worker loops
between committed units of work and drains the persist queue before exiting;
`docker stop -t` should stay comfortably above DRAIN_TIMEOUT_SECONDS for the
same reason scripts/refresh_corpus.py's own docstring gives for its drain: a
SIGKILL that lands mid-drain abandons whatever was still queued, silently.

ENVIRONMENT. Everything this script itself reads directly. app/services/seeder.py
reads its own settings (SEEDER_REGIONS, SEEDER_INTERVAL_HOURS,
SEEDER_REQUEST_DELAY, SEEDER_NEW_RELEASES_INTERVAL_HOURS,
SEEDER_REFRESH_ENABLED) through app.core.config as it always has; none of
those are read here.

    DATABASE_URL               the same database the app uses, read by
                               app.db.session at import time through the
                               app's own settings. The image entrypoint runs
                               `alembic upgrade head` against it before this
                               script gets control.
    AUDIBLE_PROXY_URL          this run's own dedicated exit. Its hostname
                               must contain "seeder" -- _verify_dedicated_proxy
                               checks for that substring and nothing else, the
                               same convention scripts/backfill_chapters.py
                               ("backfill") and scripts/refresh_corpus.py
                               ("refresh") already commit to for their own
                               exits.
    SEEDER_DRAIN_TIMEOUT_SECONDS
                               300.0   bound on waiting for the persist queue
                                       to empty at shutdown. A drain that
                                       doesn't finish within this is reported
                                       and the process still exits (non-zero;
                                       see main's exit-code comment) rather
                                       than hanging past what `docker stop -t`
                                       will wait for.
    LOG_LEVEL                  INFO    DEBUG, INFO, WARNING or ERROR. WARNING
                                       and above drop this script's own
                                       progress lines, which are logged at
                                       INFO. DEBUG=true forces DEBUG whatever
                                       this says.
    AXIOM_TOKEN                (unset) set means every line of this run ships
                                       to Axiom as well as stdout. Copying
                                       another stack's environment is the easy
                                       way to inherit it without meaning to.
    AXIOM_DATASET               libex  the dataset those lines land in.
    LOG_RETENTION_DAYS          7      rotation of the log file inside the
                                       container; 0 keeps everything.
"""

# Standard library
import argparse
import asyncio
import os
import signal
import time

# Third party
import httpx

# Database
from app.db.session import engine

# Core
from app.core.config import check_retired_env_vars
from app.core.logging import get_logger, setup_logging

# Services
from app.services.db import persist_queue
from app.services.seeder import run_new_releases_seeder, run_seeder

logger = get_logger()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# Bound on waiting for the persist queue to drain at shutdown -- see the
# module docstring's persist-queue section for why a drain is needed at all.
DRAIN_TIMEOUT_SECONDS = _env_float("SEEDER_DRAIN_TIMEOUT_SECONDS", 300.0)


# --- proxy containment (unchanged pattern; see LIBEX_LESSONS_HARD_WON.md) ---

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
    same public address the live service answers on. A value copied from
    another stack points this run at whatever exit that stack uses, and if
    that is the live service's own shared exit, six workers' worth of request
    volume (the exact failure mode this script exists to remove) lands right
    back on it under a different name.

    Checked against the hostname, not a literal URL, because the real
    production proxy value is infrastructure this app never carries in
    source and has no secret to compare against. "seeder" in the hostname is
    this script's own convention, the same way scripts/backfill_chapters.py
    and scripts/refresh_corpus.py each commit to their own word in their
    module docstrings and RUN IT examples -- an operator who leaves the
    variable unset, or reuses the shared/live value, fails this on hostname
    alone, before a single request goes out.

    The failure message names the hostname only, never the full value: proxy=
    credentials embedded in the URL must never reach a log line or an
    exception message, so hostname extraction goes through
    _proxy_host_for_log rather than a bare httpx.URL(proxy).host, which turns
    a malformed value into "(unparseable)" instead of letting InvalidURL
    escape as an uncaught traceback that skips the deliberate SystemExit
    below.

    Logged, not just raised: SystemExit propagates straight out of the
    process without ever touching the libex logger, so on its own it would
    survive only as stderr text -- in a container that runs unattended, the
    highest-severity startup condition this script has would be the one
    piece of evidence that never reaches the rotating file handler or Axiom.
    """
    proxy = os.environ.get("AUDIBLE_PROXY_URL", "")
    host = _proxy_host_for_log(proxy) if proxy else ""
    if not proxy or "seeder" not in host:
        detail = f"host {host!r}" if proxy else "unset"
        logger.error(
            "Seeder: refusing to start, AUDIBLE_PROXY_URL does not name "
            "a seeder-dedicated exit",
            extra={"proxy_host": host or "unset", "proxy_configured": bool(proxy)},
        )
        raise SystemExit(
            f"AUDIBLE_PROXY_URL ({detail}) does not name a "
            f"seeder-dedicated exit. Refusing to start against what may be "
            f"the shared production proxy or the container's own direct "
            "egress -- point this at the dedicated seeder exit before "
            "starting."
        )


# --- graceful stop -----------------------------------------------------------

class _Stopper:
    """
    Cancels every tracked task, once, on SIGTERM/SIGINT.

    run_seeder and run_new_releases_seeder loop with a bare `while True` and
    poll no stop flag of their own -- app/main.py's lifespan tore them down
    with Task.cancel() on shutdown, and this is the same mechanism applied
    from a standalone process, since nothing else here can unwind them. Each
    signal handler call is synchronous (Python requires that of anything
    registered through signal.signal), and Task.cancel() is itself a
    non-blocking, immediately-safe call to make from one -- it only schedules
    a CancelledError at the task's next suspension point, it does not run
    that task's cleanup itself. Idempotent: a second signal while the first
    is still being handled logs nothing further and calls cancel() again on
    tasks that are, by then, most likely already done.
    """

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self.requested = False

    def track(self, task: asyncio.Task) -> None:
        self._tasks.append(task)

    def request(self, *_args) -> None:
        if not self.requested:
            logger.info("Seeder: stop requested, cancelling the seeder loops")
        self.requested = True
        for task in self._tasks:
            if not task.done():
                task.cancel()


async def _drain_persist_queue(timeout: float) -> bool:
    """
    Waits for persist_queue's backlog to empty. Returns whether it actually
    did.

    See the module docstring's persist-queue section for why this exists at
    all: both workers write through persist_queue's fire-and-forget
    background tasks, which cancelling run_seeder/run_new_releases_seeder
    does not reach, and asyncio.run() abandons anything still pending when
    the coroutine it was given returns. Mirrors
    scripts/refresh_corpus.py's own _drain_persist_queue, which answers the
    identical question against the identical module.
    """
    started = time.monotonic()
    while persist_queue.queued_books() > 0:
        if time.monotonic() - started > timeout:
            return False
        logger.info(
            "Seeder: draining the persist queue",
            extra={"queued_books": persist_queue.queued_books()},
        )
        await asyncio.sleep(2.0)
    return True


# --- the run -----------------------------------------------------------------

async def _run(once: bool) -> int:
    # Dies here, before anything else, if AUDIBLE_PROXY_URL doesn't name this
    # run's own dedicated exit -- see _verify_dedicated_proxy.
    _verify_dedicated_proxy()

    logger.info(
        "Seeder: standalone run starting",
        extra={"proxy_host": _proxy_host_for_log(os.environ.get("AUDIBLE_PROXY_URL")), "once": once},
    )

    # Tasks are created and tracked BEFORE the signal handlers are registered,
    # not after: a signal landing in the gap would call stopper.request()
    # against an empty task list -- requested=True gets set, but there is
    # nothing yet to cancel, and the tasks created afterward are never told.
    # Registering last closes that window instead of guarding against it.
    stopper = _Stopper()
    seeder_task = asyncio.create_task(run_seeder(once=once), name="seeder")
    releases_task = asyncio.create_task(run_new_releases_seeder(once=once), name="seeder-new-releases")
    stopper.track(seeder_task)
    stopper.track(releases_task)
    signal.signal(signal.SIGTERM, stopper.request)
    signal.signal(signal.SIGINT, stopper.request)

    # A worker's own internal try/except already absorbs a cycle failure and
    # logs it (see app/services/seeder.py's run_seeder and
    # run_new_releases_seeder), so anything that still escapes to here is
    # either a cancellation from _Stopper or a genuine bug in the worker
    # itself -- both are worth distinguishing in the exit code below, not
    # swallowed by gather.
    results = await asyncio.gather(seeder_task, releases_task, return_exceptions=True)
    failures = [
        exc for exc in results
        if isinstance(exc, BaseException) and not isinstance(exc, asyncio.CancelledError)
    ]
    for exc in failures:
        logger.error(
            "Seeder: a worker exited with an unhandled exception",
            extra={"error_type": type(exc).__name__, "error": str(exc)},
        )

    # Both workers have stopped queuing new writes at this point (cancelled,
    # or -- under --once -- finished on their own), but writes already
    # in-flight through persist_queue's fire-and-forget tasks are not
    # awaited by either worker -- see the module docstring's persist-queue
    # section for why this wait is the only thing that reaches them.
    drained = await _drain_persist_queue(DRAIN_TIMEOUT_SECONDS)

    await engine.dispose()

    if failures:
        logger.error("Seeder: ABORTED", extra={"reason": f"{len(failures)} worker(s) raised"})
    if not drained:
        logger.error(
            "Seeder: exiting with the persist queue still non-empty",
            extra={"queued_books": persist_queue.queued_books()},
        )
    logger.info(
        "Seeder: standalone run stopped",
        extra={"stop_requested": stopper.requested, "queue_drained": drained},
    )

    # 0: clean (finished --once, or a plain stop request that drained fully).
    # 1: aborted -- a worker raised something its own try/except didn't
    # already absorb. 2: stopped without aborting but the persist queue
    # didn't drain within DRAIN_TIMEOUT_SECONDS -- distinct from 1 so a
    # supervisor can tell "a worker broke" apart from "finished, but check
    # the log for what didn't land", the same split
    # scripts/refresh_corpus.py's own exit code makes for the identical queue.
    if failures:
        return 1
    if not drained:
        return 2
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the seeder's author/series/narrator expansion and new-releases scan as a standalone process."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single supervised cycle of each worker, then exit, instead of looping forever.",
    )
    args = parser.parse_args()

    # get_logger only fetches the logger. Without this the handlers are never
    # attached and a standalone script emits nothing at all.
    setup_logging()

    # Same call app/main.py's lifespan makes, run here for the same reason:
    # this container is the one an operator is most likely to still have a
    # stale SEEDER_ENABLED set on, and without this call that variable would
    # warn about nothing here even though the CHANGELOG says it's safe to
    # leave and self-explaining. Must run after setup_logging() -- a warning
    # logged before handlers are attached goes nowhere -- and before the run
    # starts.
    check_retired_env_vars()

    exit_code = asyncio.run(_run(args.once))
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
