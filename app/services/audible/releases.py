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

THE SCAN — why it's a per-genre fan-out, not a single walk (the walk itself
lives in libex_core.audible.releases). Audible exposes no
direct new-releases or coming-soon endpoint, so we reconstruct the list from the
catalog. Every /catalog/products query is hard-capped at ~535 results regardless
of how it's filtered, and a parent-category query is NOT a superset of its
children — it's the same capped sample (measured: one parent returned ~550 while
its children unioned to ~6,300, and each level deeper escapes the cap again). So
we fetch the taxonomy (/catalog/categories?root=Genres) and flatten EVERY node at
EVERY level — the tree runs up to five levels deep — then the live scan walks a
single category by id sorted by -ReleaseDate, applying the window's date gate plus
a duplicate-page wall stop. The per-genre results are unioned and deduped by ASIN,
then sorted for the response. The full node list is stored in catalog_genres and
refreshed from Audible lazily, on the first /categories call that finds the
stored copy older than _GENRE_FRESHNESS_SECONDS (see ensure_genres) — no
background task.
"""

# Standard library
import time
from datetime import datetime, timezone
from typing import Any

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Core
from libex_core.audible.books import settle_flags_list
from libex_core.audible.client import as_audible_failure
from libex_core.audible.releases import (
    fetch_catalog_genres,
    fetch_coming_soon,
    fetch_new_releases,
)
from app.core.logging import get_logger
from app.core.utils import seconds_until_utc_midnight

# Services
from app.services.audible import audible_get
from app.services.db.reader import get_stored_genres
from app.services.db.persist_queue import persist_books_background
from app.services.db.writer import upsert_genres, reconcile_genres
from app.services.cache import manager as cache

logger = get_logger()

# When a fresh taxonomy fetch comes back this fraction (or more) of what's
# already stored, it's treated as complete enough to reconcile against — stale
# nodes get pruned. A fetch smaller than this is treated as a partial/truncated
# response: we still add what it returned, but we don't prune, so a transient
# Audible glitch can't wipe real branches out of the stored tree.
_GENRE_RECONCILE_MIN_FRACTION = 0.5

# How long a stored genre taxonomy is served without going back to Audible.
#
# One day, chosen against the two costs. Staleness costs almost nothing — and
# this is a freshness floor over a store that's never emptied, not a cache TTL,
# which is what actually makes that true rather than merely likely: Audible
# restructures its category tree rarely, a category id that already exists keeps
# working, and ensure_genres returns the stored set on every path that has one,
# including a total fetch failure, so the worst case of a too-long window is a
# newly added category showing up in /categories up to a day late — never a 404, never a
# shrunken response. "The taxonomy barely moves" only explains why staleness is
# rare; it's the never-empty store that explains why staleness is harmless when
# it happens anyway, and that second half is the one that actually justifies the
# window. Refetching costs a lot: /categories is public and unauthenticated, so
# without a gate every single request is an outbound Audible call plus a
# reconcile against the whole tree — unbounded work driven by whoever is
# calling. A day is a floor, not a cap: there's no single-flight here, so every
# request that lands against an expired region also fetches, and the true bound
# is eleven regions times whatever concurrency is live at each one's expiry, not
# eleven a day. It still turns unbounded per-request work into at most one
# fetch per region per day absent concurrent expiry, and matches the daily
# rhythm the rest of this module already runs on (the release-window caches
# expire at the next UTC midnight).
_GENRE_FRESHNESS_SECONDS = 24 * 60 * 60


def _genre_age_seconds(oldest_checked: datetime | None) -> float | None:
    """
    Returns how many seconds ago the stored taxonomy was last confirmed against
    Audible, or None when that can't be established — which is the case both
    when nothing is stored for the region and when the read failed. None means
    "no evidence of freshness", so callers must treat it as stale and fetch;
    reading it as fresh would leave an empty region empty forever.

    A naive timestamp is read as UTC rather than allowed to raise, since a
    freshness check is not worth failing a request over — and if that
    assumption is ever wrong, it errs toward serving stale data (a timestamp
    from a zone ahead of UTC reads as younger than it is), never toward a
    spurious refetch.
    """
    if oldest_checked is None:
        return None
    if oldest_checked.tzinfo is None:
        oldest_checked = oldest_checked.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - oldest_checked).total_seconds()


async def ensure_genres(session: AsyncSession, region: str) -> list[dict[str, str]]:
    """
    Returns the catalog genre nodes (every node at every level, each with its
    parent_id) for a region, refreshing them from Audible only when the stored
    copy has gone stale, and reconciling the refresh into the store so the stored
    tree mirrors Audible's current one.

    FRESHNESS. The store carries a last_checked per node; get_stored_genres hands
    back the oldest of them for the region, which is the age of the weakest part
    of that region's tree. While that age is under _GENRE_FRESHNESS_SECONDS the
    stored set is served as-is and Audible is not called at all — the taxonomy is
    near-static and /categories is public, so a request-driven refetch is work
    nobody asked for. An unknown age (nothing stored yet, or the read failed)
    counts as stale, so a region with an empty store always fetches. Everything
    here is scoped to the one region passed in: the timestamp is read for that
    region, the fetch is made against that region's marketplace, and the write is
    keyed by it, so a fresh tree in one region never suppresses a fetch in
    another.

    THE REFRESH. The fetch is a single fast taxonomy request that returns the
    whole tree at once. When it comes back and looks complete (at least
    _GENRE_RECONCILE_MIN_FRACTION of what's already stored), it's reconciled:
    new nodes are added, existing ones refreshed, and stale placements are pruned
    — so when Audible restructures (e.g. moves a category to a new parent), the
    old placement doesn't linger as a ghost. A fetch that comes back suspiciously
    small (below that fraction) is treated as partial and only added, never
    pruned, so a transient glitch can't wipe real branches. Pruning also needs a
    non-empty pre-fetch stored set: an empty read can't be told from a failed
    one, so it is written additively. On a fetch failure,
    nothing is written and a non-empty stored set is served unchanged, so an
    Audible hiccup doesn't empty the response. The result is what the /categories
    discovery endpoint serves.

    A failed fetch with nothing stored is the one case with no answer to fall
    back on: returning the empty set would read as "Audible has no categories"
    when the truth is that we could not ask, so it raises AudibleAPIException
    (via as_audible_failure) for the caller to report as an outage. Only the
    Audible fetch is converted that way: a failure writing the taxonomy to the
    store is logged, the stored set is served, and with an empty store the
    freshly fetched nodes are returned instead. When the re-read after a
    successful write comes back empty, the fetched nodes are served only if
    nothing was stored before; otherwise the previously stored set is served.
    """
    stored, oldest_checked = await get_stored_genres(session, region)
    age = _genre_age_seconds(oldest_checked)
    if stored and age is not None and age < _GENRE_FRESHNESS_SECONDS:
        logger.info(
            "Served genre taxonomy from fresh store",
            extra={
                "region": region,
                "nodes": len(stored),
                "age_seconds": round(age, 2),
                "freshness_seconds": _GENRE_FRESHNESS_SECONDS,
            },
        )
        return stored

    try:
        nodes = await fetch_catalog_genres(audible_get, region)
    except Exception as e:
        logger.warning(
            "Genre taxonomy fetch failed",
            extra={"region": region, "error": str(e)},
        )
        if not stored:
            raise as_audible_failure(e, "Audible genre taxonomy fetch failed") from e
        return stored

    if nodes:
        # Audible answered; a failure from here on is ours, not Audible's, so
        # it is logged and never relabelled as an upstream outage.
        try:
            # Pruning needs a known non-empty pre-fetch set to size the fetch
            # against. An empty read is ambiguous: get_stored_genres returns
            # the same ([], None) for a region with nothing stored and for a
            # failed read, and an unknown store must never be read as an empty
            # one, or any partial fetch would clear the 0.5 floor and prune
            # real branches. Additive upsert is identical to reconcile when the
            # store really is empty, so nothing is lost by taking it.
            if stored and len(nodes) >= _GENRE_RECONCILE_MIN_FRACTION * len(stored):
                # Plausibly complete — mirror Audible's current tree, pruning
                # any stale placements (the ghost-root case).
                await reconcile_genres(session, region, nodes)
            else:
                # Suspiciously small, or the stored size is unknown — add what
                # we got, but don't prune.
                if not stored:
                    logger.warning(
                        "Genre taxonomy stored set empty or unreadable; writing additively",
                        extra={"region": region, "fetched_nodes": len(nodes)},
                    )
                await upsert_genres(session, region, nodes)
            await session.commit()
            reread, _ = await get_stored_genres(session, region)
            if reread:
                stored = reread
            elif not stored:
                # The write committed but the re-read came back empty or
                # failed; the fetched nodes are in hand and an empty list
                # would claim there are no genres.
                logger.warning(
                    "Genre taxonomy re-read empty after commit; serving fetched nodes",
                    extra={"region": region, "fetched_nodes": len(nodes)},
                )
                return nodes
        except Exception as e:
            logger.warning(
                "Genre taxonomy store failed",
                extra={"region": region, "error": str(e)},
            )
            # With nothing stored, what Audible just returned is the only
            # honest answer; an empty list would claim there are no genres.
            if not stored:
                return nodes

    return stored


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

    A failed Audible scan raises AudibleAPIException rather than returning an
    empty list. Persistence and cache write failures are logged and the books
    are still returned.
    """
    key = cache.new_releases_key(region, days, category)
    cached = await cache.get(session, key)
    if cached is not None:
        return cached

    try:
        start = time.monotonic()
        books = await fetch_new_releases(audible_get, region, days, category)
    except Exception as e:
        logger.error(
            "New releases scan failed",
            extra={"region": region, "category": category, "error": str(e)},
        )
        # An empty list here would be indistinguishable from Audible
        # answering that nothing released in the window; the failure is
        # raised so the route can report an outage instead. Only the walk
        # is converted -- a persistence or cache failure below is ours, not
        # Audible's, and never reaches this handler.
        raise as_audible_failure(e, "Audible new releases scan failed") from e

    took = round((time.monotonic() - start) * 1000, 2)
    logger.info("Requested Audible new releases", extra={
        "region": region,
        "days": days,
        "category": category,
        "results": len(books),
        "took": took,
    })

    if books:
        # Unsettled: the writer needs the tri-state flags None/True/False
        # exactly as normalize_product produced them (see libex_core.storage.write.support.asserted_bool), so this runs before the settle below.
        try:
            persist_books_background(books, region)
        except Exception as e:
            logger.error(
                "New releases persist failed",
                extra={"region": region, "category": category, "error": str(e)},
            )
        # This endpoint's cache is read-through and returned as-is on a
        # hit (see module docstring), unlike books.py's own cache, which
        # is re-settled on every read regardless of source -- so what's
        # cached and returned here has to already be the settled value.
        books = settle_flags_list(books)
        try:
            await cache.set(session, key, books, ttl_seconds=seconds_until_utc_midnight())
        except Exception as e:
            # The scan succeeded; a lost cache write costs a re-scan on the
            # next request, not the answer.
            logger.error(
                "New releases cache write failed",
                extra={"region": region, "category": category, "error": str(e)},
            )
    return books


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

    A failed Audible scan raises AudibleAPIException rather than returning an
    empty list. Persistence and cache write failures are logged and the books
    are still returned.
    """
    key = cache.coming_soon_key(region, days, category)
    cached = await cache.get(session, key)
    if cached is not None:
        return cached

    try:
        start = time.monotonic()
        books = await fetch_coming_soon(audible_get, region, days, category)
    except Exception as e:
        logger.error(
            "Coming soon scan failed",
            extra={"region": region, "category": category, "error": str(e)},
        )
        # An empty list here would be indistinguishable from Audible
        # answering that nothing released in the window; the failure is
        # raised so the route can report an outage instead. Only the walk
        # is converted -- a persistence or cache failure below is ours, not
        # Audible's, and never reaches this handler.
        raise as_audible_failure(e, "Audible coming soon scan failed") from e

    took = round((time.monotonic() - start) * 1000, 2)
    logger.info("Requested Audible coming soon", extra={
        "region": region,
        "days": days,
        "category": category,
        "results": len(books),
        "took": took,
    })

    if books:
        # Unsettled: the writer needs the tri-state flags None/True/False
        # exactly as normalize_product produced them (see libex_core.storage.write.support.asserted_bool), so this runs before the settle below.
        try:
            persist_books_background(books, region)
        except Exception as e:
            logger.error(
                "Coming soon persist failed",
                extra={"region": region, "category": category, "error": str(e)},
            )
        # This endpoint's cache is read-through and returned as-is on a
        # hit (see module docstring), unlike books.py's own cache, which
        # is re-settled on every read regardless of source -- so what's
        # cached and returned here has to already be the settled value.
        books = settle_flags_list(books)
        try:
            await cache.set(session, key, books, ttl_seconds=seconds_until_utc_midnight())
        except Exception as e:
            # The scan succeeded; a lost cache write costs a re-scan on the
            # next request, not the answer.
            logger.error(
                "Coming soon cache write failed",
                extra={"region": region, "category": category, "error": str(e)},
            )
    return books
