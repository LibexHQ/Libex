"""
Standalone seeder entry point.

Runs the seeder's author/series/narrator expansion and new-releases scan as
one process in its own container, on its own dedicated exit. app/main.py's
lifespan used to start these as background tasks in every worker process,
which meant six uncoordinated walks of the same catalog and Audible traffic
at six times the configured pace. Running this container is the enable
decision -- there is no separate flag.

RUN IT (its own container, its own dedicated exit -- AUDIBLE_PROXY_URL must
name it explicitly; the run refuses to start unless its hostname contains
"seeder", see _verify_dedicated_proxy). docker-compose.seeder.yml is the
canonical way to run this: its own Portainer stack, its own VPN exit, and
its own DATABASE_URL, which reaches Postgres at libex-postgres:5432 over
libex-db, the network the API stack creates and this stack joins as external.

    docker compose -f docker-compose.seeder.yml run --rm libex-seeder \\
      python -m scripts.seed --once

--once runs a single supervised cycle of each worker then exits, instead of
looping forever -- use it for a supervised, watch-it invocation; the compose
file's own `command:` runs the forever loop.

Stop the long-running form with `docker stop libex-seeder` (or
`docker compose -f docker-compose.seeder.yml stop`) -- SIGTERM cancels both
worker loops between committed units of work and drains the persist queue
before exiting. The stack's `stop_grace_period: 310s` covers that; a shorter
grace would SIGKILL mid-drain and silently abandon queued writes. A drain
that doesn't finish within SEEDER_DRAIN_TIMEOUT_SECONDS is reported and the
process exits non-zero anyway (see main's exit-code comment).

ENVIRONMENT. app/services/seeder.py reads its own settings (SEEDER_REGIONS,
SEEDER_INTERVAL_HOURS, SEEDER_REQUEST_DELAY, SEEDER_NEW_RELEASES_INTERVAL_HOURS,
SEEDER_REFRESH_ENABLED) through app.core.config; this script reads:

    DATABASE_URL                   required. Read by app.db.session at import.
    AUDIBLE_PROXY_URL              required. Hostname must contain "seeder".
    SEEDER_DRAIN_TIMEOUT_SECONDS   300.0   bound on waiting for the persist
                                           queue to empty at shutdown.
    LOG_LEVEL                      INFO    DEBUG, INFO, WARNING or ERROR.
    AXIOM_TOKEN                    (unset) set to also ship logs to Axiom.
    AXIOM_DATASET                   libex
    LOG_RETENTION_DAYS              7      0 keeps everything.
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


# Bound on waiting for the persist queue to drain at shutdown -- both
# workers write through persist_queue's fire-and-forget background tasks,
# which cancellation never reaches, so this wait is what does.
DRAIN_TIMEOUT_SECONDS = _env_float("SEEDER_DRAIN_TIMEOUT_SECONDS", 300.0)


# --- proxy containment (unchanged pattern; see LIBEX_LESSONS_HARD_WON.md) ---

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
    contains "seeder" -- otherwise this script's Audible traffic would
    egress from the container's own address, the same one the live service
    answers on. Checked against the hostname only, since the real proxy
    value may carry embedded credentials and must never reach a log line or
    exception message. Logged before the SystemExit, since SystemExit alone
    never reaches the log handlers.
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
    poll no stop flag of their own, so this is the same Task.cancel()
    mechanism app/main.py's lifespan used to apply on shutdown. Idempotent:
    a second signal calls cancel() again on tasks already done by then.
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

    Both workers write through persist_queue's fire-and-forget background
    tasks, which cancelling run_seeder/run_new_releases_seeder never reaches,
    and asyncio.run() abandons anything still pending when the coroutine it
    was given returns -- this wait is what closes that gap.
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

    # A worker's own internal try/except already absorbs a cycle failure, so
    # anything that still escapes to here is a cancellation from _Stopper or
    # a genuine bug -- worth distinguishing in the exit code, not swallowed.
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
    # in-flight through persist_queue's fire-and-forget tasks are awaited by
    # neither -- this wait is what reaches them.
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
    # didn't drain within DRAIN_TIMEOUT_SECONDS.
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

    # Warns if a stale SEEDER_ENABLED is still set. Must run after
    # setup_logging() -- a warning logged before handlers are attached goes
    # nowhere -- and before the run starts.
    check_retired_env_vars()

    exit_code = asyncio.run(_run(args.once))
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
