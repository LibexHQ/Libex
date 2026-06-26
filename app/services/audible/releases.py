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
its children unioned to ~6,300, and each level deeper escapes the cap again). So
we fetch the taxonomy (/catalog/categories?root=Genres) and flatten EVERY node at
EVERY level — the tree runs up to five levels deep — then the live scan walks a
single category by id sorted by -ReleaseDate, applying the window's date gate plus
a duplicate-page wall stop. The per-genre results are unioned and deduped by ASIN,
then sorted for the response. The full node list is stored in catalog_genres,
fetched fresh from Audible on each /categories call and reconciled to match (see
_ensure_genres) — no background task.
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
from app.services.db.writer import persist_books_background, upsert_genres, reconcile_genres
from app.services.cache import manager as cache

settings = get_settings()
logger = get_logger()

_PAGE_SIZE = 50

# When a fresh taxonomy fetch comes back this fraction (or more) of what's
# already stored, it's treated as complete enough to reconcile against — stale
# nodes get pruned. A fetch smaller than this is treated as a partial/truncated
# response: we still add what it returned, but we don't prune, so a transient
# Audible glitch can't wipe real branches out of the stored tree.
_GENRE_RECONCILE_MIN_FRACTION = 0.5


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
    Fetches the genre taxonomy from Audible and flattens every node to a list,
    each tagged with its parent_id.

    The taxonomy is a tree up to five levels deep and ragged — some branches stop
    at two levels, some go five — so the flatten recurses to whatever depth
    Audible returns (requested via categories_num_levels). A top-level parent gets
    parent_id="" ; every other node gets its parent's id. A node that appears under
    two parents yields one row per parent. Deduped by (genre_id, parent_id). This
    populates the /categories discovery surface; the live scan walks a single
    category by id.
    """
    data = await audible_get(
        region,
        "/1.0/catalog/categories",
        {"root": "Genres", "categories_num_levels": 5},
    )
    seen: set[tuple[str, str]] = set()
    nodes: list[dict[str, str]] = []

    def emit(node_list: list[dict], parent_id: str) -> None:
        for n in node_list:
            nid = n.get("id")
            name = n.get("name")
            if nid and name and (nid, parent_id) not in seen:
                seen.add((nid, parent_id))
                nodes.append({"genre_id": nid, "name": name, "parent_id": parent_id})
            if nid:
                emit(n.get("children", []), nid)

    emit(data.get("categories", []), "")
    return nodes


async def _ensure_genres(session: AsyncSession, region: str) -> list[dict[str, str]]:
    """
    Returns the catalog genre nodes (every node at every level, each with its
    parent_id) for a region, fetched fresh from Audible on every call and
    reconciled into the store so the stored tree mirrors Audible's current one.

    The fetch is a single fast taxonomy request that returns the whole tree at
    once. When it comes back and looks complete (at least
    _GENRE_RECONCILE_MIN_FRACTION of what's already stored), it's reconciled:
    new nodes are added, existing ones refreshed, and stale placements are pruned
    — so when Audible restructures (e.g. moves a category to a new parent), the
    old placement doesn't linger as a ghost. A fetch that comes back suspiciously
    small (below that fraction) is treated as partial and only added, never
    pruned, so a transient glitch can't wipe real branches. On a fetch failure,
    nothing is written and the stored set is served unchanged, so an Audible
    hiccup doesn't empty the response. Either way the stored set is returned,
    which is what the /categories discovery endpoint serves.
    """
    stored, _ = await get_stored_genres(session, region)
    try:
        nodes = await _fetch_catalog_genres(region)
        if nodes:
            if len(nodes) >= _GENRE_RECONCILE_MIN_FRACTION * len(stored):
                # Plausibly complete — mirror Audible's current tree, pruning
                # any stale placements (the ghost-root case).
                await reconcile_genres(session, region, nodes)
            else:
                # Suspiciously small — add what we got, but don't prune.
                await upsert_genres(session, region, nodes)
            await session.commit()
            stored, _ = await get_stored_genres(session, region)
    except Exception as e:
        logger.warning(f"Genre taxonomy fetch failed for {region}: {e}")

    return stored


async def _walk_one_catalog(
    region: str,
    category_id: str | None,
    collect,
    should_stop,
) -> list[dict[str, Any]]:
    """
    Walks a single catalog query sorted by -ReleaseDate (descending), deduped by
    ASIN. When category_id is given, the walk is scoped to that one category;
    when it's None, the walk is the un-categoried catalog (the bare-call
    "sample" — Audible caps it at ~535, so it's a slice, not the full set).

    `collect(dt)` decides whether a book is in-window; `should_stop(dt)` decides
    when the descending walk has passed the window's near edge. The walk stops at
    the first book satisfying should_stop, when a page repeats the previous one
    (Audible's ~535-result wall — a consecutive repeat), or when a page comes
    back short/empty. Books with no parseable date are skipped. No inter-request
    delay — this is the live path, and a single category returns in time.
    """
    collected: dict[str, dict[str, Any]] = {}
    page = 0
    prev_asins: list[str] | None = None
    while True:
        params: dict[str, Any] = {
            "num_results": _PAGE_SIZE,
            "page": page,
            "response_groups": BOOK_RESPONSE_GROUPS,
            "image_sizes": IMAGE_SIZES,
            "products_sort_by": "-ReleaseDate",
        }
        if category_id:
            params["category_id"] = category_id
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
    category: str | None = None,
) -> list[dict[str, Any]]:
    """
    Returns books released in the last `days`, newest first, scanned live from
    Audible. Cache-first with a TTL to the next UTC midnight (see module
    docstring). Already-released only — future pre-orders are skipped.

    With a `category` id (from /categories), the scan is scoped to that one
    category and returns the full window for it. Without one, the scan is the
    un-categoried catalog — Audible caps that at a few hundred results, so the
    bare call returns a live sample, not the full catalog (use a category, or
    the DB endpoints, for completeness).
    """
    key = cache.new_releases_key(region, days, category)
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
        books = await _walk_one_catalog(region, category, collect, should_stop)
        books.sort(
            key=lambda b: _release_dt(b) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        took = round((time.monotonic() - start) * 1000, 2)
        logger.info("Requested Audible new releases", extra={
            "region": region,
            "days": days,
            "category": category,
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
    category: str | None = None,
) -> list[dict[str, Any]]:
    """
    Returns upcoming books releasing in the next `days`, soonest first, scanned
    live from Audible. Cache-first with a TTL to the next UTC midnight (see
    module docstring). Future releases only.

    With a `category` id (from /categories), the scan is scoped to that one
    category and returns the full window for it. Without one, the scan is the
    un-categoried catalog — Audible caps that at a few hundred results, so the
    bare call returns a live sample, not the full catalog (use a category, or
    the DB endpoints, for completeness).
    """
    key = cache.coming_soon_key(region, days, category)
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
        books = await _walk_one_catalog(region, category, collect, should_stop)
        books.sort(key=lambda b: _release_dt(b) or datetime.max.replace(tzinfo=timezone.utc))
        took = round((time.monotonic() - start) * 1000, 2)
        logger.info("Requested Audible coming soon", extra={
            "region": region,
            "days": days,
            "category": category,
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