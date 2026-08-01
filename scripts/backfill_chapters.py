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
  chapters_checked_at is set, whether we found chapters or Audible had none. So
  the run is fully resumable — stop and restart any time and it picks up where it
  left off, and books with no chapters are never re-fetched.
- Rate is a small random delay per request (averaging ~1/s, never bursting) plus
  a macro on/off schedule (~12h active, then a random pause), so the traffic
  isn't mechanically uniform.
- On sustained non-404 errors (the signature of the IP getting throttled) it
  backs off, and if it stays bad it pauses loudly rather than hammering.

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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Database
from app.db.models import Book, Track

# Core
from app.core.logging import get_logger

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

# back-off: if the last WINDOW attempts had more than THRESHOLD non-404 errors,
# the exit IP is probably being throttled — pause hard for COOLDOWN seconds.
ERROR_WINDOW = _env_int("BACKFILL_ERROR_WINDOW", 50)
ERROR_THRESHOLD = _env_int("BACKFILL_ERROR_THRESHOLD", 25)
ERROR_COOLDOWN = _env_float("BACKFILL_ERROR_COOLDOWN", 1800.0)  # 30 min

# how often to log a progress line
PROGRESS_EVERY = _env_int("BACKFILL_PROGRESS_EVERY", 100)

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
    STORED = "stored"       # chapters fetched and saved
    NONE = "none"           # Audible has no chapters for this book
    ERROR = "error"         # transient/other failure — do NOT mark, retry later


async def _process_one(session: AsyncSession, asin: str, region: str) -> str:
    """
    Fetches and stores one book's chapters.

    Returns an _Outcome. On STORED/NONE the book is marked checked (leaves the
    queue). On ERROR nothing is marked, so it'll be retried on a later pass.
    """
    try:
        data = await audible_get(region, CHAPTERS_PATH.format(asin=asin), CHAPTERS_PARAMS)
    except Exception as e:
        # network / Audible error — transient. Leave unmarked for retry.
        logger.warning(f"Backfill: fetch failed for {asin} ({region}): {type(e).__name__}: {e}")
        return _Outcome.ERROR

    # Same "no chapters" check the on-demand path uses.
    if not data.get("content_metadata", {}).get("chapter_info"):
        await _mark_checked(session, asin)
        return _Outcome.NONE

    try:
        chapters = _normalize_chapters(data, asin)
        await _store_chapters(session, asin, chapters)
        await _mark_checked(session, asin)
        return _Outcome.STORED
    except Exception as e:
        # write failure — leave unmarked so we retry.
        logger.warning(f"Backfill: store failed for {asin}: {type(e).__name__}: {e}")
        await session.rollback()
        return _Outcome.ERROR


# --- the run ---------------------------------------------------------------

async def _run(limit: int | None) -> None:
    proxy = os.environ.get("AUDIBLE_PROXY_URL", "(none)")
    logger.info(
        "Backfill: starting",
        extra={
            "proxy": proxy,
            "limit": limit if limit is not None else "all",
            "delay_range": f"{DELAY_MIN}-{DELAY_MAX}s",
            "active_hours": ACTIVE_HOURS,
        },
    )

    db_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(db_url, pool_size=2, max_overflow=2)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    stopper = _Stopper()
    signal.signal(signal.SIGTERM, stopper.request)
    signal.signal(signal.SIGINT, stopper.request)

    processed = stored = none = errors = 0
    recent_errors: list[bool] = []  # rolling window of "was this attempt an error"
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

                    outcome = await _process_one(session, asin, region)
                    processed += 1
                    is_error = outcome == _Outcome.ERROR
                    if outcome == _Outcome.STORED:
                        stored += 1
                    elif outcome == _Outcome.NONE:
                        none += 1
                    elif is_error:
                        errors += 1

                    # rolling error window for back-off
                    recent_errors.append(is_error)
                    if len(recent_errors) > ERROR_WINDOW:
                        recent_errors.pop(0)

                    if processed % PROGRESS_EVERY == 0:
                        rate = (sum(recent_errors) / len(recent_errors) * 100) if recent_errors else 0
                        logger.info(
                            "Backfill: progress",
                            extra={
                                "processed": processed,
                                "stored": stored,
                                "no_chapters": none,
                                "errors": errors,
                                "recent_error_rate": f"{rate:.0f}%",
                            },
                        )

                    # back-off: too many recent errors => IP probably throttled
                    if (
                        len(recent_errors) >= ERROR_WINDOW
                        and sum(recent_errors) >= ERROR_THRESHOLD
                    ):
                        logger.error(
                            f"Backfill: {sum(recent_errors)}/{len(recent_errors)} recent "
                            f"attempts failed — pausing {ERROR_COOLDOWN / 60:.0f}m (exit IP "
                            f"may be throttled)"
                        )
                        await _interruptible_sleep(ERROR_COOLDOWN, stopper)
                        recent_errors.clear()
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
    asyncio.run(_run(args.limit))


if __name__ == "__main__":
    main()