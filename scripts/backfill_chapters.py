"""
One-off chapter backfill.

Walks books that haven't had their chapters fetched yet (chapters_checked_at IS
NULL), pulls each book's chapters from Audible, and stores them — gently, over a
long stretch, so it doesn't look like a scraper and doesn't get the exit IP
flagged.

Design notes:
- Routes Audible calls through whatever AUDIBLE_PROXY_URL is set for THIS process
  (meant to be a dedicated VPN exit, separate from the live app's), so if this
  IP gets throttled the live service is unaffected.
- Reuses the app's own fetch/normalize/write path (audible_get, the chapter
  normalizer, upsert_track) so stored data is identical to on-demand fetches.
- The work queue is self-checkpointing: a book leaves it the moment
  chapters_checked_at is set, whether we stored chapters, found none, or Audible
  404s the record. So the run is fully resumable and never re-fetches a book it
  has already resolved.
- A 404 (NotFoundException) is terminal: some records — notably ISBN-keyed ones
  (~7% of the catalog, non-B ASINs) — return no chapter metadata in ANY region,
  confirmed by cross-region probing. Retrying them would waste requests, never
  drain the queue, and falsely trip the error back-off. So a 404 is marked
  checked and counted as "not found", NOT as an error.
- A confirmed-permanent non-404 status is terminal too, the same way: currently
  just 400, since that's the only one actually observed — the same ~43 ASINs
  answered 400 on every cycle across six days of production logs, the set
  growing monotonically (2 to 43) and never clearing on retry. client.py's
  AudibleAPIException carries the real upstream HTTP status
  (`upstream_status`; `None` for a timeout or connection failure, which never
  reached Audible at all), so this script marks a permanent status checked and
  counts it as "permanent" rather than looping it forever as a generic error.
- The rolling back-off window counts every failure that plausibly means the
  exit IP itself is in trouble: a 429, a 5xx, a timeout or connection failure
  (the latter two arrive as `upstream_status is None`), and a 401/403. 401/403
  is deliberately left off the *permanent* list — Libex sends no Authorization
  header, so an anonymous call getting a 401/403 is far more likely a real IP
  or proxy block than a fact about that ASIN, and marking the ASIN checked
  would permanently lose it once the block clears — and that exact same
  reasoning is why it DOES feed the back-off window: it's the
  plausible-IP-trouble case the window exists to catch. Checked directly
  against the real API rather than assumed: `GET /1.0/library` with no
  credentials returns 403 "Request could not be authenticated" — an
  account/session-level rejection, not a per-title one — while genuine
  per-ASIN restrictions surface as a descriptive 400 instead (confirmed live
  for the ASINs this queue actually produces one for: "is a non_audio asset
  with contentDeliveryType:Bundle"). Excluded from the window: confirmed
  per-record facts (a 404, a confirmed-permanent status like 400), and any
  failure that never became an `AudibleAPIException` at all, so there is no
  `upstream_status` to classify — a 200 whose body failed to parse, a local
  write error. Pausing fixes none of those.
- Rate is a small random delay per request (averaging ~1/s, never bursting) plus
  a macro on/off schedule (~12h active, then a random pause), so the traffic
  isn't mechanically uniform.

Run as a separate container off the Libex image:

    docker run -d --name libex-chapter-backfill \\
      --network libex-proxy \\
      -e AUDIBLE_PROXY_URL=http://libex-backfill-vpn:8888 \\
      -e DATABASE_URL=<same as the app> \\
      ghcr.io/libexhq/libex:latest \\
      python scripts/backfill_chapters.py --limit 5     # dry run; drop --limit for the real run

Stop with `docker stop libex-chapter-backfill` — it finishes the current book,
commits, and exits cleanly.
"""

# Standard library
import argparse
import asyncio
import os
import random
import signal
import time
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


# per-request delay is a random draw in [MIN, MAX] seconds — averages ~1/s and
# never bursts, so the peak rate can't trip a short-window threshold.
DELAY_MIN = _env_float("BACKFILL_DELAY_MIN", 0.7)
DELAY_MAX = _env_float("BACKFILL_DELAY_MAX", 2.0)

# macro on/off: run for ACTIVE_HOURS, then pause a random number of hours in
# [PAUSE_MIN, PAUSE_MAX], then resume. Breaks up the daily traffic signature.
ACTIVE_HOURS = _env_float("BACKFILL_ACTIVE_HOURS", 12.0)
PAUSE_MIN_HOURS = _env_float("BACKFILL_PAUSE_MIN_HOURS", 4.0)
PAUSE_MAX_HOURS = _env_float("BACKFILL_PAUSE_MAX_HOURS", 11.0)

# how many books to pull from the queue per DB round-trip (memory/txn scope only,
# not a rate knob).
CHUNK_SIZE = _env_int("BACKFILL_CHUNK_SIZE", 500)

# back-off: if the last WINDOW attempts had more than THRESHOLD failures where
# pausing is a plausible remedy (NOT 404s, NOT a confirmed-permanent status
# like 400 — see _is_backoff_signal), the exit IP is probably being
# throttled — pause hard for COOLDOWN.
ERROR_WINDOW = _env_int("BACKFILL_ERROR_WINDOW", 50)
ERROR_THRESHOLD = _env_int("BACKFILL_ERROR_THRESHOLD", 25)
ERROR_COOLDOWN = _env_float("BACKFILL_ERROR_COOLDOWN", 1800.0)  # 30 min

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
            logger.info("Backfill: stop requested, will exit after the current book")
        self.stopping = True


# --- core operations -------------------------------------------------------

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
    """One-time startup check: confirm which IP our proxy actually exits from."""
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


async def _next_batch(session: AsyncSession, limit: int) -> list[tuple[str, str]]:
    """Returns up to `limit` (asin, region) pairs that still need checking."""
    result = await session.execute(
        select(Book.asin, Book.region)
        .where(Book.chapters_checked_at.is_(None))
        .order_by(Book.asin)
        .limit(limit)
    )
    return [(row[0], row[1]) for row in result.all()]


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
    NOT_FOUND = "not_found"  # 404 — record has no fetchable chapters anywhere (terminal)
    PERMANENT = "permanent"  # confirmed-permanent non-404 status (currently just 400) — terminal
    ERROR = "error"          # unresolved failure; retried, and counts toward the plain error total


# Non-404 upstream statuses treated as a permanent fact about the specific
# record rather than a failure worth retrying. Limited to 400 — see the
# module docstring above for the evidence (six days of production logs, the
# ~43-ASIN set, and the live-confirmed "non_audio asset with
# contentDeliveryType:Bundle" message). 410 would belong here too if Audible
# ever returned it for this endpoint, but there's zero evidence of that, so
# it isn't added speculatively. 401/403 are deliberately NOT here — see
# _is_backoff_signal and the module docstring for why they're kept
# retryable instead.
_PERMANENT_UPSTREAM_STATUSES = {400}


def _is_backoff_signal(upstream_status: int | None) -> bool:
    """True for every failure that plausibly means the exit IP itself is in
    trouble, rather than a fact about the specific ASIN: no response at all
    (a timeout or connection failure, which arrives as upstream_status is
    None), a 429, a 5xx, or a 401/403. The 401/403 case was checked against
    the real API rather than assumed: GET /1.0/library with no credentials
    returns 403 "Request could not be authenticated" — an account/session-
    level rejection, not a per-title one — while every per-ASIN restriction
    found instead surfaces as a descriptive 400 (see
    _PERMANENT_UPSTREAM_STATUSES). So a 401/403 here is exactly the class of
    failure this window exists to catch, not a per-record fact to exclude
    from it. Excluded: confirmed per-record facts (404, and any status in
    _PERMANENT_UPSTREAM_STATUSES) — those never reach this function at all,
    since _process_one returns before calling it for those cases."""
    if upstream_status is None or upstream_status in (401, 403, 429):
        return True
    return 500 <= upstream_status < 600


async def _process_one(session: AsyncSession, asin: str, region: str) -> tuple[str, bool]:
    """
    Fetches and stores one book's chapters.

    Outcomes:
    - STORED / NONE / NOT_FOUND / PERMANENT: the book is marked checked and
      leaves the queue. These mean, respectively: chapters saved; resolved
      with none; a 404; a confirmed-permanent non-404 status (see
      _PERMANENT_UPSTREAM_STATUSES). None of these count as an error.
    - ERROR: an unresolved failure. Left unmarked so it's retried on a later
      pass, and always counts toward the plain error total.

    Returns (outcome, is_backoff_signal) — the second element is only ever
    True alongside ERROR, and only when _is_backoff_signal says pausing the
    whole run is a plausible remedy for this particular failure.
    """
    try:
        data = await audible_get(region, CHAPTERS_PATH.format(asin=asin), CHAPTERS_PARAMS)
    except NotFoundException:
        # 404 — terminal. This record has no chapter metadata (confirmed across
        # all regions for the ISBN-keyed class). Mark it and move on; do not retry
        # and do not treat as an error.
        await _mark_checked(session, asin)
        return _Outcome.NOT_FOUND, False
    except AudibleAPIException as e:
        if e.upstream_status in _PERMANENT_UPSTREAM_STATUSES:
            # Confirmed permanent per-ASIN fact — see _PERMANENT_UPSTREAM_STATUSES.
            # Mark it and move on exactly like a 404: no retry, not an error.
            await _mark_checked(session, asin)
            return _Outcome.PERMANENT, False
        logger.warning(f"Backfill: fetch error for {asin} ({region}): {type(e).__name__}: {e}")
        return _Outcome.ERROR, _is_backoff_signal(e.upstream_status)
    except Exception as e:
        # Never became an AudibleAPIException, so there is no upstream_status
        # to classify (audible_get returned 200 and the body failed to parse,
        # a bug, etc.) — always retried, but never a back-off signal, since
        # pausing the crawl fixes nothing about it.
        logger.warning(f"Backfill: fetch error for {asin} ({region}): {type(e).__name__}: {e}")
        return _Outcome.ERROR, False

    # Resolved, but no chapters present.
    if not data.get("content_metadata", {}).get("chapter_info"):
        await _mark_checked(session, asin)
        return _Outcome.NONE, False

    try:
        chapters = _normalize_chapters(data, asin)
        await _store_chapters(session, asin, chapters)
        await _mark_checked(session, asin)
        return _Outcome.STORED, False
    except Exception as e:
        # write failure — leave unmarked so we retry. Not a back-off signal
        # either: a local write failure says nothing about the exit IP.
        logger.warning(f"Backfill: store failed for {asin}: {type(e).__name__}: {e}")
        await session.rollback()
        return _Outcome.ERROR, False


# --- the run ---------------------------------------------------------------

async def _run(limit: int | None) -> None:
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
            "delay_range": f"{DELAY_MIN}-{DELAY_MAX}s",
            "active_hours": ACTIVE_HOURS,
        },
    )
    await _log_exit_ip()

    db_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(db_url, pool_size=2, max_overflow=2)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    stopper = _Stopper()
    signal.signal(signal.SIGTERM, stopper.request)
    signal.signal(signal.SIGINT, stopper.request)

    processed = stored = none = not_found = permanent = errors = 0
    # rolling window of "does this attempt's failure make pausing a plausible
    # remedy" — NOT "was this attempt a failure"; see _is_backoff_signal.
    recent_backoff_signals: list[bool] = []
    active_until = time.monotonic() + ACTIVE_HOURS * 3600

    try:
        while not stopper.stopping:
            # macro schedule: if the active window is up, pause a random spell.
            if time.monotonic() >= active_until:
                pause = random.uniform(PAUSE_MIN_HOURS, PAUSE_MAX_HOURS) * 3600
                logger.info(f"Backfill: active window done, pausing ~{pause / 3600:.1f}h")
                await _interruptible_sleep(pause, stopper)
                if stopper.stopping:
                    break
                active_until = time.monotonic() + ACTIVE_HOURS * 3600

            # pull the next batch of work
            remaining = None if limit is None else max(0, limit - processed)
            batch_size = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
            if batch_size == 0:
                logger.info("Backfill: reached --limit, stopping")
                break

            async with Session() as session:
                batch = await _next_batch(session, batch_size)
                if not batch:
                    logger.info("Backfill: no more books need chapters — done!")
                    break

                for asin, region in batch:
                    if stopper.stopping:
                        break

                    outcome, is_backoff_signal = await _process_one(session, asin, region)
                    processed += 1
                    if outcome == _Outcome.STORED:
                        stored += 1
                    elif outcome == _Outcome.NONE:
                        none += 1
                    elif outcome == _Outcome.NOT_FOUND:
                        not_found += 1
                    elif outcome == _Outcome.PERMANENT:
                        permanent += 1
                    elif outcome == _Outcome.ERROR:
                        errors += 1

                    # rolling back-off window — only failures pausing could
                    # plausibly fix enter it; see _is_backoff_signal. A 404, a
                    # confirmed-permanent status (e.g. 400), and a failure
                    # that never became an AudibleAPIException (a parse or
                    # local write error) all leave this False; everything
                    # that did become one is classified by _is_backoff_signal.
                    recent_backoff_signals.append(is_backoff_signal)
                    if len(recent_backoff_signals) > ERROR_WINDOW:
                        recent_backoff_signals.pop(0)

                    if processed % PROGRESS_EVERY == 0:
                        signals = recent_backoff_signals
                        rate = (sum(signals) / len(signals) * 100) if signals else 0
                        logger.info(
                            "Backfill: progress",
                            extra={
                                "processed": processed,
                                "stored": stored,
                                "no_chapters": none,
                                "not_found": not_found,
                                "permanent": permanent,
                                "errors": errors,
                                "recent_backoff_signal_rate": f"{rate:.0f}%",
                            },
                        )

                    # back-off: pause once the window is dominated by failures
                    # pausing could plausibly fix. This observes a rate, it
                    # doesn't diagnose a cause — see _is_backoff_signal for
                    # exactly which failures are eligible.
                    if (
                        len(recent_backoff_signals) >= ERROR_WINDOW
                        and sum(recent_backoff_signals) >= ERROR_THRESHOLD
                    ):
                        logger.error(
                            f"Backfill: {sum(recent_backoff_signals)}/{len(recent_backoff_signals)} "
                            f"recent requests hit a pause-worthy failure — "
                            f"pausing {ERROR_COOLDOWN / 60:.0f}m"
                        )
                        await _interruptible_sleep(ERROR_COOLDOWN, stopper)
                        recent_backoff_signals.clear()
                        if stopper.stopping:
                            break

                    # gentle, jittered pacing between requests
                    await _interruptible_sleep(random.uniform(DELAY_MIN, DELAY_MAX), stopper)
    finally:
        await engine.dispose()
        logger.info(
            "Backfill: stopped",
            extra={
                "processed": processed,
                "stored": stored,
                "no_chapters": none,
                "not_found": not_found,
                "permanent": permanent,
                "errors": errors,
            },
        )


async def _interruptible_sleep(seconds: float, stopper: _Stopper) -> None:
    """Sleeps in short slices so a stop request is noticed promptly."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and not stopper.stopping:
        await asyncio.sleep(min(1.0, end - time.monotonic()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill book chapters from Audible.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N books then stop (for a dry run). Omit to process all.",
    )
    args = parser.parse_args()
    # Wire up the app's log handlers (stdout/stderr/file) — get_logger only
    # fetches the logger; without this the handlers aren't attached and nothing
    # is emitted when run as a standalone script.
    setup_logging()
    asyncio.run(_run(args.limit))


if __name__ == "__main__":
    main()