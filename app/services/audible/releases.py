"""
Audible release-window services: new releases and coming soon.

These two endpoints invert Libex's usual Audible-first cache policy, and do so
deliberately. Every other service calls Audible first and treats the cache as a
fallback for when Audible is down. These two read the cache FIRST and only scan
Audible on a miss.

The reason it's safe: both endpoints answer a date-windowed question over
date-only release data ("books releasing in the next/last N days"). That answer
cannot change until the calendar date rolls over — within a single UTC day a
cached response is byte-identical to a fresh scan. So we cache until the next
UTC midnight (see seconds_until_utc_midnight) and refresh lazily on the first
request of the new day. This serves the freshest possible answer while turning
an otherwise per-request catalog scan into at most one scan per window/region
per day.

The scan itself walks Audible's catalog sorted by -ReleaseDate (descending) and
stops based on the dates it sees rather than a fixed page count, so it returns
every book in the requested window. A hard page cap guards against runaway
paging if the data ever misbehaves.
"""

# Standard library
import time
from datetime import datetime, timedelta, timezone
from typing import Any

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Core
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.utils import seconds_until_utc_midnight

# Services
from app.services.audible.client import audible_get
from app.services.audible.books import (
    _normalize_product,
    _filter_products,
    BOOK_RESPONSE_GROUPS,
    IMAGE_SIZES,
)
from app.services.db.writer import persist_books_background
from app.services.cache import manager as cache

settings = get_settings()
logger = get_logger()

# Safety cap on how many catalog pages a single scan will walk. The date-based
# stop condition normally fires well before this; it's here so a data anomaly
# can't loop forever.
_MAX_PAGES = 50
_PAGE_SIZE = 50


def _release_dt(book: dict[str, Any]) -> datetime | None:
    """Parses a normalized book's releaseDate back into a datetime, or None."""
    raw = book.get("releaseDate")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def _scan_by_release_date(
    region: str,
    collect,
    should_stop,
) -> list[dict[str, Any]]:
    """
    Walks the catalog sorted by -ReleaseDate (descending), normalizing each
    page. `collect(dt)` decides whether a book is in-window; `should_stop(dt)`
    decides when the descending scan has passed the window's near edge.

    Books with no parseable date are skipped. Paging stops at the first book
    that satisfies should_stop, when a page comes back short, or at the page
    cap — whichever comes first.
    """
    collected: list[dict[str, Any]] = []
    page = 0
    while page < _MAX_PAGES:
        params: dict[str, Any] = {
            "num_results": _PAGE_SIZE,
            "page": page,
            "response_groups": BOOK_RESPONSE_GROUPS,
            "image_sizes": IMAGE_SIZES,
            "sort_by": "-ReleaseDate",
        }
        data = await audible_get(region, "/1.0/catalog/products/", params)
        products = _filter_products(data.get("products", []))
        if not products:
            break

        stop = False
        for product in products:
            book = _normalize_product(product, region)
            dt = _release_dt(book)
            if dt is None:
                continue
            if should_stop(dt):
                stop = True
                break
            if collect(dt):
                collected.append(book)

        if stop:
            break
        if len(products) < _PAGE_SIZE:
            break
        page += 1

    return collected


async def get_new_releases(
    region: str,
    session: AsyncSession,
    days: int = 30,
) -> list[dict[str, Any]]:
    """
    Returns books released in the last `days`, newest first, scanned live from
    Audible. Cache-first with a TTL to the next UTC midnight (see module
    docstring). Already-released only — future pre-orders are skipped.
    """
    key = cache.new_releases_key(region, days)
    cached = await cache.get(session, key)
    if cached is not None:
        return cached

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)

    # Descending scan: skip future (> now), collect in-window, stop once we
    # descend past the window's old edge.
    def collect(dt: datetime) -> bool:
        return window_start <= dt <= now

    def should_stop(dt: datetime) -> bool:
        return dt < window_start

    try:
        start = time.monotonic()
        books = await _scan_by_release_date(region, collect, should_stop)
        took = round((time.monotonic() - start) * 1000, 2)
        logger.info("Requested Audible new releases", extra={
            "region": region,
            "days": days,
            "results": len(books),
            "took": took,
        })

        if books:
            persist_books_background(books, region)
            await cache.set(session, key, books, ttl_seconds=seconds_until_utc_midnight())
        return books

    except Exception as e:
        logger.error(f"New releases scan failed: {e}")
        return []


async def get_coming_soon(
    region: str,
    session: AsyncSession,
    days: int = 30,
) -> list[dict[str, Any]]:
    """
    Returns upcoming books releasing in the next `days`, soonest first, scanned
    live from Audible. Cache-first with a TTL to the next UTC midnight (see
    module docstring). Future releases only.
    """
    key = cache.coming_soon_key(region, days)
    cached = await cache.get(session, key)
    if cached is not None:
        return cached

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=days)

    # Descending scan: skip far-future (> window_end), collect in-window, stop
    # once we descend to already-released (<= now).
    def collect(dt: datetime) -> bool:
        return now < dt <= window_end

    def should_stop(dt: datetime) -> bool:
        return dt <= now

    try:
        start = time.monotonic()
        books = await _scan_by_release_date(region, collect, should_stop)
        # Scan came in newest-first (descending); coming soon wants soonest-first.
        books.sort(key=lambda b: _release_dt(b) or datetime.max.replace(tzinfo=timezone.utc))
        took = round((time.monotonic() - start) * 1000, 2)
        logger.info("Requested Audible coming soon", extra={
            "region": region,
            "days": days,
            "results": len(books),
            "took": took,
        })

        if books:
            persist_books_background(books, region)
            await cache.set(session, key, books, ttl_seconds=seconds_until_utc_midnight())
        return books

    except Exception as e:
        logger.error(f"Coming soon scan failed: {e}")
        return []