"""
Audible books service.
Fetches book metadata directly from the Audible API.

DESIGN PHILOSOPHY: Audible-first.
Always fetches fresh data from Audible.
Writes every result to the relational DB for persistence.
Falls back to DB when Audible is unavailable.
"""

# Standard library
import time
from datetime import datetime, timezone
from typing import Any

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Core
from app.core.exceptions import NotFoundException
from app.core.logging import get_logger
from app.core.utils import strip_html, strip_image_size_suffix

# Services
from app.services.audible.client import audible_get, REGION_MAP
from app.services.cache import manager as cache
from app.services.cache.manager import book_key, chapters_key
from app.services.db.writer import upsert_book, upsert_track
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
    """Filters out unreleased placeholder products."""
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
) -> list[dict[str, Any]]:
    """
    Fetches one or more books by ASIN from Audible.
    Writes results to relational DB and cache.
    Falls back to DB then cache when Audible is unavailable.
    """
    if not asins:
        raise NotFoundException("No ASINs provided")

    seen: set[str] = set()
    unique_asins = [a for a in asins if not (a in seen or seen.add(a))]  # type: ignore

    if use_cache and len(unique_asins) == 1:
        cached = await cache.get(session, book_key(unique_asins[0], region))
        if cached:
            return [cached]

    try:
        start = time.monotonic()
        chunks = [unique_asins[i:i + 50] for i in range(0, len(unique_asins), 50)]
        all_products = []
        for chunk in chunks:
            products = await _fetch_chunk(chunk, region)
            all_products.extend(products)

        requested_took = round((time.monotonic() - start) * 1000, 2)

        if not all_products:
            return []

        normalized = [_normalize_product(p, region) for p in all_products]

        # Write to DB and cache
        for book in normalized:
            if book.get("asin"):
                await upsert_book(session, book)
                await cache.set(session, book_key(book["asin"], region), book)

        logger.info("Requested books from Audible", extra={
            "requested_num": len(unique_asins),
            "requested_took": requested_took,
            "region": region,
        })

        return normalized

    except NotFoundException:
        raise

    except Exception:
        await session.rollback()
        logger.warning(f"Audible unavailable, attempting DB fallback for {unique_asins}")

        # Try relational DB first
        db_results = await get_books_from_db(session, unique_asins)
        if db_results:
            return db_results

        # Fall back to cache
        results = []
        for asin in unique_asins:
            cached = await cache.get(session, book_key(asin, region))
            if cached:
                results.append(cached)
        if results:
            return results

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

        # Write to DB and cache
        await upsert_track(session, asin, result)
        await cache.set(session, chapters_key(asin, region), result)

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