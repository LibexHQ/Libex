"""
Audible books service.
Fetches book metadata directly from the Audible API.

DESIGN PHILOSOPHY: Audible-first.
Always fetches fresh data from Audible.
Writes every result to the relational DB for persistence.
Falls back to DB when Audible is unavailable.
"""

# Standard library
import asyncio
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any

# Third party
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.models import Book

# Core
from app.core.exceptions import NotFoundException
from app.core.logging import get_logger
from app.core.utils import strip_html, strip_image_size_suffix

# Services
from app.services.audible.client import audible_get, author_books_concurrency, REGION_MAP
from app.services.cache import manager as cache
from app.services.cache.manager import book_key, chapters_key
from app.services.db.writer import persist_books_background, persist_track_background, upsert_track
from app.services.db.reader import get_books_from_db, get_track_from_db

logger = get_logger()

# ============================================================
# CONSTANTS
# ============================================================

BOOK_RESPONSE_GROUPS = (
    "media, product_attrs, product_desc, product_details, "
    "product_extended_attrs, product_plans, rating, series, "
    "relationships, review_attrs, category_ladders, customer_rights"
)

IMAGE_SIZES = "500,1000,2400,3200"

UNRELEASED_PLACEHOLDER = "2200-01-01T00:00:00Z"

# Below this many products, normalization runs inline on the event loop; at
# or above it, the whole batch is handed to a single asyncio.to_thread call.
# Measured (scratchpad benchmark, not part of this repo): a lone product
# normalizes in ~0.2ms and a 50-ASIN chunk -- the largest a single Audible
# catalog request ever returns, see the chunking below -- in ~4-5ms, while
# the to_thread hop itself costs ~0.3-0.5ms of fixed overhead regardless of
# batch size. Every book/series/search caller (single ASIN up to one chunk)
# stays under this threshold and keeps its current inline timing exactly.
# It's only get_author_books' hydration, which accumulates products across
# many chunks before normalizing once, that can cross it -- the flagged
# ~1530-product case blocks the loop for ~150ms inline with nothing else
# able to run in that window, against ~180ms threaded but with the loop free
# to service other requests throughout. The thread hop is a net wall-clock
# loss at every size tested; the point is solely to stop a single request
# from stalling every other connection the process is holding.
NORMALIZE_THREAD_THRESHOLD = 100


# ============================================================
# HELPERS
# ============================================================

def _best_image(product_images: dict | None) -> str | None:
    """Returns the highest resolution image URL with size suffix stripped."""
    if not product_images:
        return None
    highest_key = max((int(k) for k in product_images if k.isdigit()), default=None)
    if highest_key is None:
        return None
    url = product_images.get(str(highest_key))
    return strip_image_size_suffix(url)


def _audible_link(asin: str, region: str) -> str:
    """Builds an Audible product page link."""
    tld = REGION_MAP.get(region, ".com")
    return f"https://audible{tld}/pd/{asin}"

def _parse_release_date(raw: str | None) -> str | None:
    """
    Converts a raw Audible release date string to ISO 8601 format.
    Audimeta stores dates as DateTime and outputs .toISO(), e.g. "2021-03-02T00:00:00.000+00:00".
    """
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return raw


def _parse_authors(product: dict, region: str) -> list[dict]:
    """Extracts author objects matching AudiMeta's MinimalAuthorDto."""
    authors = []
    for author in product.get("authors", []):
        name = author.get("name", "").replace("\t", "").strip()
        asin = author.get("asin", "").replace("\t", "").strip() if author.get("asin") else None
        if asin and len(asin) > 12:
            asin = None
        if name:
            authors.append({
                "id": None,
                "asin": asin,
                "name": name,
                "region": region,
                "regions": [region],
                "image": None,
                "updatedAt": None,
            })
    return authors


def _parse_narrators(product: dict) -> list[dict]:
    """Extracts narrator objects matching AudiMeta's NarratorDto."""
    return [
        {"name": n.get("name", "").strip(), "updatedAt": None}
        for n in product.get("narrators", [])
        if n.get("name")
    ]


def _parse_genres(product: dict) -> list[dict]:
    """Extracts genre objects matching AudiMeta's GenreDto with type and betterType."""
    genres = []
    seen = set()
    for ladder in product.get("category_ladders", []):
        for rung_index, rung in enumerate(ladder.get("ladder", [])):
            name = rung.get("name")
            asin = rung.get("id")
            if name and name not in seen:
                seen.add(name)
                genre_type = "Genres" if rung_index == 0 else "Tags"
                genres.append({
                    "asin": asin,
                    "name": name,
                    "type": genre_type,
                    "betterType": genre_type.lower().rstrip("s"),
                    "updatedAt": None,
                })
    return genres


def _parse_plans(product: dict) -> list[str]:
    """Extracts plan_names from the plans array."""
    return [p["plan_name"] for p in product.get("plans", []) if p.get("plan_name")]


def _parse_series(product: dict, region: str) -> list[dict]:
    """Extracts series objects matching AudiMeta's MinimalSeriesDto."""
    series_list = []
    for item in product.get("relationships", []):
        if item.get("relationship_type") == "series":
            series_list.append({
                "asin": item.get("asin"),
                "name": item.get("title"),
                "position": item.get("sequence"),
                "region": region,
                "updatedAt": None,
            })
    return series_list


def _normalize_product(product: dict, region: str) -> dict[str, Any]:
    """
    Normalizes a raw Audible product into Libex response format.
    Field names match AudiMeta's BookDto exactly for drop-in compatibility.
    """
    asin = product.get("asin", "")
    series_list = _parse_series(product, region)

    content_type = product.get("content_type")
    is_podcast = content_type and content_type.lower() == "podcast"

    return {
        "asin": asin,
        "title": product.get("title"),
        "subtitle": product.get("subtitle"),
        "description": strip_html(product.get("merchandising_summary")),
        "summary": strip_html(product.get("publisher_summary")),
        "region": region,
        "regions": [region],
        "publisher": product.get("publisher_name"),
        "copyright": product.get("copyright"),
        "isbn": product.get("isbn"),
        "language": product.get("language"),
        "rating": product.get("rating", {}).get("overall_distribution", {}).get("average_rating"),
        "bookFormat": product.get("format_type"),
        "releaseDate": _parse_release_date(product.get("release_date")),
        "explicit": product.get("is_adult_product", False),
        "hasPdf": product.get("is_pdf_url_available", False),
        "whisperSync": product.get("read_along_support", False),
        "imageUrl": _best_image(product.get("product_images", {})),
        "lengthMinutes": product.get("runtime_length_min"),
        "link": _audible_link(asin, region),
        "contentType": content_type,
        "contentDeliveryType": product.get("content_delivery_type"),
        "episodeNumber": str(product.get("episode_number")) if is_podcast and product.get("episode_number") else None,
        "episodeType": product.get("episode_type") if is_podcast else None,
        "sku": product.get("sku"),
        "skuGroup": product.get("sku_lite"),
        "isListenable": product.get("is_listenable", False),
        "isAvailable": product.get("is_buyable", False),
        "isBuyable": product.get("is_buyable", False),
        "isVvab": product.get("is_vvab", False),
        "plans": _parse_plans(product),
        "updatedAt": None,
        "authors": _parse_authors(product, region),
        "narrators": _parse_narrators(product),
        "genres": _parse_genres(product),
        "series": series_list,
    }


async def _normalize_products(products: list[dict], region: str) -> list[dict[str, Any]]:
    """
    Normalizes a batch of raw Audible products, offloading the whole batch
    to a worker thread when it's large enough to be worth the hop (see
    NORMALIZE_THREAD_THRESHOLD). One thread hop for the entire batch, never
    one per product -- per-product hops would each pay the hop's own fixed
    cost on top of the ~0.2ms of work being moved, which loses badly at the
    hundreds-to-low-thousands sizes this exists for.

    _normalize_product touches only its own arguments and pure helpers
    (strip_html, strip_image_size_suffix, datetime parsing) -- no DB session,
    no cache, no shared mutable state, and no read of any ContextVar, so
    running it on another thread carries no correctness risk. In particular
    it never reads the author_books_concurrency ContextVar in client.py,
    which wouldn't propagate into a to_thread worker the way a normal await
    does -- moot here since nothing in this path looks at it.

    Ordering matches the input list either way (a single list comprehension,
    run inline or inside one to_thread call), and a malformed product raises
    out of that comprehension exactly as it always has -- offloading doesn't
    change which products succeed or fail relative to each other, only which
    thread does the work.
    """
    if len(products) < NORMALIZE_THREAD_THRESHOLD:
        return [_normalize_product(p, region) for p in products]
    return await asyncio.to_thread(lambda: [_normalize_product(p, region) for p in products])


def _normalize_chapters(data: dict, asin: str) -> dict[str, Any]:
    """Normalizes raw Audible chapter data into AudiMeta's TrackContentDto format."""
    chapter_info = data.get("content_metadata", {}).get("chapter_info", {})
    raw_chapters = chapter_info.get("chapters", [])

    chapters = [
        {
            "lengthMs": c.get("length_ms", 0),
            "startOffsetMs": c.get("start_offset_ms", 0),
            "startOffsetSec": c.get("start_offset_sec", 0),
            "title": c.get("title", ""),
        }
        for c in raw_chapters
    ]

    return {
        "brandIntroDurationMs": chapter_info.get("brandIntroDurationMs", 0),
        "brandOutroDurationMs": chapter_info.get("brandOutroDurationMs", 0),
        "isAccurate": chapter_info.get("is_accurate", False),
        "runtimeLengthMs": chapter_info.get("runtime_length_ms", 0),
        "runtimeLengthSec": chapter_info.get("runtime_length_sec", 0),
        "chapters": chapters,
    }


def _filter_products(products: list[dict]) -> list[dict]:
    """Filters out unreleased placeholder products.

    Also load-bearing for resolving a nonexistent-but-well-formed ASIN to
    "not found": probed live, Audible's catalog endpoints don't 404 for one
    of those in either _fetch_chunk branch -- they return 200 with a
    hollow, titleless stub instead (a bogus single ASIN and a bogus ASIN
    in a batch request both came back that way; a real ASIN came back
    fully populated). Dropping anything with no title here is what turns
    that stub into an empty result rather than a phantom book with every
    field blank.
    """
    return [
        p for p in products
        if p.get("title")
        and p.get("publication_datetime") != UNRELEASED_PLACEHOLDER
    ]


# ============================================================
# CHUNKING
# ============================================================

async def _fetch_chunk(asins: list[str], region: str) -> list[dict[str, Any]]:
    """Fetches a single chunk of up to 50 ASINs from Audible."""
    if not asins:
        return []

    if len(asins) == 1:
        path = f"/1.0/catalog/products/{asins[0]}"
        params: dict[str, Any] = {
            "response_groups": BOOK_RESPONSE_GROUPS,
            "image_sizes": IMAGE_SIZES,
        }
        data = await audible_get(region, path, params)
        products = [data.get("product", {})] if data.get("product") else []
    else:
        path = "/1.0/catalog/products"
        params = {
            "asins": ",".join(asins),
            "response_groups": BOOK_RESPONSE_GROUPS,
            "image_sizes": IMAGE_SIZES,
        }
        data = await audible_get(region, path, params)
        products = data.get("products", [])

    return _filter_products(products)


# ============================================================
# PUBLIC API
# ============================================================

async def get_books_by_asins(
    asins: list[str],
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
    high_concurrency: bool = False,
) -> list[dict[str, Any]]:
    """
    Fetches one or more books by ASIN from Audible.
    Writes results to relational DB and cache.
    Falls back to DB then cache when Audible is unavailable.

    high_concurrency, when True, runs the chunk fan-out below inside
    author_books_concurrency() (see client.py), drawing from the wider
    AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT pool instead of the default one.
    Set only by the author-ASIN routes hydrating get_author_books' own
    result -- the one path where a single request legitimately fans out to
    dozens of chunk requests and a live, measured production outage
    (5 concurrent author lookups 504ing at the fronting proxy's 30s timeout)
    traced directly back to that fan-out being serialized behind the
    default pool. Every other caller (single/small ASIN lists from the book,
    series, and search routes, and the seeder) defaults to False and is
    unaffected.
    """
    if not asins:
        raise NotFoundException("No ASINs provided")

    seen: set[str] = set()
    unique_asins = [a for a in asins if not (a in seen or seen.add(a))]  # type: ignore

    if use_cache and len(unique_asins) == 1:
        cached = await cache.get(session, book_key(unique_asins[0], region))
        if cached:
            return [cached]

    # Batch cache: one lookup for the whole list, only fetch misses from
    # Audible. Reading the keys back in unique_asins order keeps cached_results
    # in the caller's order: that is the entire response on the all-hits early
    # return below, and its leading segment on every other return, each of
    # which concatenates it ahead of the freshly-fetched and backstop results
    # rather than reordering it.
    cached_results: list[dict[str, Any]] = []
    fetch_asins = unique_asins
    if use_cache and len(unique_asins) > 1:
        keys = [book_key(a, region) for a in unique_asins]
        hits = await cache.get_many(session, keys)
        cached_results = []
        fetch_asins = []
        for asin, key in zip(unique_asins, keys):
            hit = hits.get(key)
            if hit:
                cached_results.append(hit)
            else:
                fetch_asins.append(asin)

        # All ASINs found in cache
        if not fetch_asins:
            return cached_results

    # A connection is held for work, not for a request. Either cache read
    # above autobegins a transaction on session, and a READ COMMITTED
    # transaction advertises backend_xmin and can become the cluster's oldest
    # xmin even when it has only ever read -- measured on PostgreSQL 16.14 --
    # holding the xmin horizon against autovacuum until it ends. Held across
    # the fan-out below, that window is whatever Audible takes: the chunk
    # requests are unbounded in time and queue against the process-wide pool
    # in client.py, so a bulk request at the route's 1000-ASIN cap is 20
    # chunks draining through a handful of permits in successive waves, with
    # nothing between the cache read and the last chunk that needs a
    # transaction open. Released here, before any of it.
    #
    # One placement covers every path into the fan-out. With use_cache False
    # neither read ran, session is still lazy and holds no connection, and
    # rollback with no transaction in progress is a pass-through that touches
    # neither the session nor the pool. Nothing after this point depends on
    # transaction state established before it: both reads return plain
    # already-materialized values into cached_results, and the two later
    # session users -- the DB backstop for transiently failed chunks, and the
    # outage fallback in the except branch -- each open their own transaction
    # on their next statement, which SQLAlchemy re-acquires transparently.
    await session.rollback()

    try:
        start = time.monotonic()
        chunks = [fetch_asins[i:i + 50] for i in range(0, len(fetch_asins), 50)]

        # Fire every chunk concurrently -- audible_get itself is the throttle
        # point (a process-wide bound lives there), so nothing here needs to
        # cap fan-out. return_exceptions=True so a bad chunk can't wipe out
        # the chunks that already came back; gather still guarantees results
        # line up with chunks by index regardless of completion order, so
        # reassembly below stays in the same order the caller passed in.
        #
        # nullcontext when high_concurrency is False keeps every other
        # caller's behavior byte-identical to before this parameter existed
        # -- the pool draw only changes for the one path that opts in.
        pool_context = author_books_concurrency() if high_concurrency else nullcontext()
        with pool_context:
            results = await asyncio.gather(
                *(_fetch_chunk(chunk, region) for chunk in chunks),
                return_exceptions=True,
            )

        requested_took = round((time.monotonic() - start) * 1000, 2)

        all_products: list[dict[str, Any]] = []
        not_found_asins: list[str] = []
        transient_failed_asins: list[str] = []
        transient_errors: list[Exception] = []

        for idx, (chunk, result) in enumerate(zip(chunks, results)):
            if isinstance(result, NotFoundException):
                # Only the single-ASIN branch of _fetch_chunk can raise this
                # at all -- the batch endpoint's own response is always a 200
                # with a products array, even when every requested ASIN is
                # unknown, so a batch chunk never surfaces as an exception
                # here. A nonexistent-but-well-formed ASIN doesn't reach this
                # branch either way: probed live, Audible returns 200 with a
                # hollow, titleless stub for one of those in both branches,
                # and _filter_products (see that function) is what turns that
                # stub into nothing to add rather than a 404. Whatever does
                # reach this branch is terminal for that one ASIN regardless,
                # not a reason to discard everything else that already
                # succeeded.
                not_found_asins.extend(chunk)
                continue
            if isinstance(result, Exception):
                transient_failed_asins.extend(chunk)
                transient_errors.append(result)
                logger.warning(
                    f"Hydration chunk {idx + 1}/{len(chunks)} failed for "
                    f"{len(chunk)} ASINs ({region}): {type(result).__name__}: {result}"
                )
                continue
            all_products.extend(result)

        if not_found_asins or transient_failed_asins:
            logger.warning("Partial hydration shortfall", extra={
                "requested_num": len(fetch_asins),
                "not_found_asins": len(not_found_asins),
                "failed_asins": len(transient_failed_asins),
                "region": region,
            })

        # Every chunk that mattered failed transiently and nothing else came
        # back -- fall through to the DB/cache fallback below exactly as a
        # single sequential failure would have. A pure not-found (no transient
        # errors) does NOT take this path: 404 is terminal, not a retry signal.
        if transient_failed_asins and not all_products and not cached_results:
            raise transient_errors[0]

        if not all_products and not cached_results:
            return []

        # less-data-never-accepted for a partial transient failure: some
        # chunks succeeding (or use_cache already producing cache hits) used
        # to skip the DB backstop entirely for transient_failed_asins,
        # silently omitting whatever was already stored for exactly the
        # ASINs the failed chunk(s) covered -- a 1500-ASIN author plus one
        # upstream 503 dropped the ~50 stored books that chunk owned, and
        # use_cache=True with every chunk failing returned cache hits only.
        # Scoped to transient_failed_asins alone, never not_found_asins: a
        # 404 is a confirmed absence, not a retry signal, and must not be
        # papered over by stale DB data. Runs whenever any chunk failed
        # transiently, independent of whether all_products or cached_results
        # already have something, so neither can silently swallow it the way
        # both used to.
        db_backstop_results: list[dict[str, Any]] = []
        if transient_failed_asins:
            db_backstop_results = await get_books_from_db(session, transient_failed_asins)

        normalized = await _normalize_products(all_products, region)

        if all_products:
            # Persist to DB and cache in the background
            persist_books_background(normalized, region)

        logger.info("Requested books from Audible", extra={
            "requested_num": len(fetch_asins),
            "cache_hits": len(cached_results),
            "requested_took": requested_took,
            "not_found_asins": len(not_found_asins),
            "failed_asins": len(transient_failed_asins),
            "db_backstop_num": len(db_backstop_results),
            "region": region,
        })

        return cached_results + normalized + db_backstop_results

    except NotFoundException:
        raise

    except Exception:
        await session.rollback()
        logger.warning(f"Audible unavailable, attempting DB fallback for {fetch_asins}")

        # Try relational DB first for the misses
        db_results = await get_books_from_db(session, fetch_asins)
        if db_results:
            return cached_results + db_results

        # Fall back to cache for the misses -- one lookup, same as the
        # pre-fetch check above, and read back in fetch_asins order.
        fallback_results = []
        fallback_keys = [book_key(a, region) for a in fetch_asins]
        fallback_hits = await cache.get_many(session, fallback_keys)
        for key in fallback_keys:
            hit = fallback_hits.get(key)
            if hit:
                fallback_results.append(hit)
        if fallback_results or cached_results:
            return cached_results + fallback_results

        raise NotFoundException("Audible unavailable and no cached data found")


async def get_book_by_asin(
    asin: str,
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
) -> dict[str, Any]:
    """Fetches a single book by ASIN."""
    books = await get_books_by_asins([asin], region, session, use_cache)
    if not books:
        raise NotFoundException(f"Book not found: {asin}")
    return books[0]


async def get_chapters(
    asin: str,
    region: str,
    session: AsyncSession,
) -> dict[str, Any]:
    """
    Fetches chapter information for a book by ASIN.
    Returns data matching AudiMeta's TrackContentDto format.
    """
    try:
        path = f"/1.0/content/{asin}/metadata"
        params = {
            "response_groups": "chapter_info, always-returned, content_reference, content_url",
            "quality": "High",
        }

        start = time.monotonic()
        data = await audible_get(region, path, params)
        chapters_took = round((time.monotonic() - start) * 1000, 2)

        if not data.get("content_metadata", {}).get("chapter_info"):
            raise NotFoundException(f"No chapter information found for {asin}")

        result = _normalize_chapters(data, asin)

        # Persist to DB and cache in the background
        persist_track_background(asin, result, region)

        logger.info("Requested chapters from Audible", extra={
            "chapters_took": chapters_took,
            "region": region,
        })

        return result

    except NotFoundException:
        raise

    except Exception:
        # Try DB first
        db_result = await get_track_from_db(session, asin)
        if db_result:
            return db_result

        # Fall back to cache
        cached = await cache.get(session, chapters_key(asin, region))
        if cached:
            return cached

        raise NotFoundException("Audible unavailable and no cached chapter data found")


async def fetch_and_store_chapters(
    asin: str,
    region: str,
    session: AsyncSession,
) -> str:
    """
    Fetches a book's chapters and stores them, stamping chapters_checked_at so the
    book is recorded as checked whatever the outcome. Best-effort: this never
    raises, so a chapter failure can't break whatever persistence flow called it
    (the seeder relies on that — its job is book metadata, chapters are a bonus).

    Outcomes mirror the standalone backfill:
    - "stored":    chapters fetched and written to tracks; marked checked.
    - "none":      resolved but Audible exposes no chapters; marked checked.
    - "not_found": 404 — no chapter metadata anywhere (e.g. the ISBN-keyed
                   records); marked checked so it isn't retried.
    - "error":     transient failure (Audible 500/timeout/network) or a write
                   failure; NOT marked, so a later pass (or the backfill) retries.

    Coordinates with the standalone backfill via chapters_checked_at: a book
    marked here won't be re-fetched there, and vice versa.
    """
    path = f"/1.0/content/{asin}/metadata"
    params = {
        "response_groups": "chapter_info, always-returned, content_reference, content_url",
        "quality": "High",
    }

    try:
        data = await audible_get(region, path, params)
    except NotFoundException:
        await _mark_chapters_checked(session, asin)
        return "not_found"
    except Exception as e:
        logger.warning(f"Seeder chapters: fetch error for {asin} ({region}): {type(e).__name__}: {e}")
        return "error"

    if not data.get("content_metadata", {}).get("chapter_info"):
        await _mark_chapters_checked(session, asin)
        return "none"

    try:
        chapters = _normalize_chapters(data, asin)
        await upsert_track(session, asin, chapters)
        await _mark_chapters_checked(session, asin)
        return "stored"
    except Exception as e:
        logger.warning(f"Seeder chapters: store failed for {asin}: {type(e).__name__}: {e}")
        await session.rollback()
        return "error"


async def _mark_chapters_checked(session: AsyncSession, asin: str) -> None:
    """Stamps chapters_checked_at on a book so it leaves the backfill queue."""
    await session.execute(
        update(Book)
        .where(Book.asin == asin)
        .values(chapters_checked_at=datetime.now(timezone.utc))
    )
    await session.commit()