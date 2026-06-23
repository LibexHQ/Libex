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

THE SCAN — why it's a per-genre fan-out, not a single walk. Audible exposes no
direct new-releases or coming-soon endpoint, so we reconstruct the list from the
catalog. Every /catalog/products query is hard-capped at ~535 results regardless
of how it's filtered, and a parent-category query is NOT a superset of its
children — it's the same capped sample (measured: one parent returned ~550 while
its children unioned to ~6,300). So we walk the LEAF genres: fetch the taxonomy
(/catalog/categories?root=Genres), flatten to every sub-genre, and walk each one
sorted by -ReleaseDate, applying the window's date gate plus a duplicate-page
wall stop. The per-genre results are unioned and deduped by ASIN, then sorted for
the response. The leaf list is stored in catalog_genres and refreshed at most
once a day (inline, on a cache miss) — no background task.
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
from app.services.db.reader import get_stored_genres
from app.services.db.writer import persist_books_background, upsert_genres
from app.services.cache import manager as cache

settings = get_settings()
logger = get_logger()

_PAGE_SIZE = 50

# The leaf genre list is re-fetched from Audible at most this often. The taxonomy
# changes rarely, so a daily refresh is plenty; the check is inline (on a cache
# miss), not a background task.
_GENRE_REFRESH_INTERVAL = timedelta(hours=24)


def _release_dt(book: dict[str, Any]) -> datetime | None:
    """Parses a normalized book's releaseDate back into a datetime, or None."""
    raw = book.get("releaseDate")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def _fetch_catalog_genres(region: str) -> list[dict[str, str]]:
    """
    Fetches the genre taxonomy from Audible and flattens it to the LEAF set —
    every sub-genre across all parent categories, deduped by id.

    The response is two levels: a top-level `categories` list of parents, each
    with a `children` list of leaves (both carry `id` + `name`). We keep the
    leaves only: a parent query is capped at the same ~535 results as any other,
    so it under-returns its children's union — the leaves are what reach the full
    catalog. Some leaf ids appear under two parents, so we dedupe.
    """
    data = await audible_get(region, "/1.0/catalog/categories", {"root": "Genres"})
    seen: set[str] = set()
    leaves: list[dict[str, str]] = []
    for parent in data.get("categories", []):
        for child in parent.get("children", []):
            gid = child.get("id")
            name = child.get("name")
            if gid and name and gid not in seen:
                seen.add(gid)
                leaves.append({"genre_id": gid, "name": name})
    return leaves


async def _ensure_genres(session: AsyncSession, region: str) -> list[dict[str, str]]:
    """
    Returns the leaf genre list for a region, refreshing it from Audible at most
    once a day. Reads the stored set; if it's empty or older than the refresh
    interval, re-fetches the taxonomy and stores it. On a fetch failure, falls
    back to whatever's already stored so a transient Audible hiccup doesn't empty
    the walk.
    """
    stored, oldest_checked = await get_stored_genres(session, region)

    fresh_enough = (
        stored
        and oldest_checked is not None
        and (datetime.now(timezone.utc) - oldest_checked) < _GENRE_REFRESH_INTERVAL
    )
    if fresh_enough:
        return stored

    try:
        leaves = await _fetch_catalog_genres(region)
        if leaves:
            await upsert_genres(session, region, leaves)
            await session.commit()
            return leaves
    except Exception as e:
        logger.warning(f"Genre taxonomy refresh failed for {region}: {e}")

    # Fetch failed or returned nothing — use whatever we have stored.
    return stored


async def _scan_genres_by_release_date(
    region: str,
    genres: list[dict[str, str]],
    collect,
    should_stop,
) -> list[dict[str, Any]]:
    """
    Walks each leaf genre's catalog sorted by -ReleaseDate (descending) and
    unions the results, deduped by ASIN. `collect(dt)` decides whether a book is
    in-window; `should_stop(dt)` decides when the descending walk has passed the
    window's near edge.

    Each genre walk stops at the first book satisfying should_stop, when a page
    repeats the previous one (Audible's ~535-result wall — proven to be a
    consecutive repeat), or when a page comes back short/empty. Books with no
    parseable date are skipped. No inter-request delay — this is the live path.
    """
    collected: dict[str, dict[str, Any]] = {}

    for genre in genres:
        page = 0
        prev_asins: list[str] | None = None
        while True:
            params: dict[str, Any] = {
                "category_id": genre["genre_id"],
                "num_results": _PAGE_SIZE,
                "page": page,
                "response_groups": BOOK_RESPONSE_GROUPS,
                "image_sizes": IMAGE_SIZES,
                "products_sort_by": "-ReleaseDate",
            }
            data = await audible_get(region, "/1.0/catalog/products/", params)
            products = _filter_products(data.get("products", []))
            if not products:
                break

            # Duplicate-page wall: Audible repeats the last page once it runs out.
            page_asins = [p.get("asin") for p in products]
            if page_asins == prev_asins:
                break
            prev_asins = page_asins

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
                    asin = book.get("asin")
                    if asin:
                        collected[asin] = book

            if stop:
                break
            if len(products) < _PAGE_SIZE:
                break
            page += 1

    return list(collected.values())


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
        genres = await _ensure_genres(session, region)
        books = await _scan_genres_by_release_date(region, genres, collect, should_stop)
        # Union of many genre walks — sort newest-first for the response.
        books.sort(
            key=lambda b: _release_dt(b) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        took = round((time.monotonic() - start) * 1000, 2)
        logger.info("Requested Audible new releases", extra={
            "region": region,
            "days": days,
            "genres": len(genres),
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
        genres = await _ensure_genres(session, region)
        books = await _scan_genres_by_release_date(region, genres, collect, should_stop)
        # Union of many genre walks; coming soon wants soonest-first.
        books.sort(key=lambda b: _release_dt(b) or datetime.max.replace(tzinfo=timezone.utc))
        took = round((time.monotonic() - start) * 1000, 2)
        logger.info("Requested Audible coming soon", extra={
            "region": region,
            "days": days,
            "genres": len(genres),
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