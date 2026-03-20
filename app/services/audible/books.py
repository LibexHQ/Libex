"""
Audible books service.
Fetches book metadata directly from the Audible API.

DESIGN PHILOSOPHY: Audible-first.
Always fetches fresh data from Audible.
Cache is used only as a fallback when Audible is unavailable.
"""

# Standard library
import re
from typing import Any

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Core
from app.core.exceptions import NotFoundException
from app.core.logging import get_logger

# Services
from app.services.audible.client import audible_get
from app.services.cache import manager as cache
from app.services.cache.manager import book_key, chapters_key

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

def _best_image(product_images: dict) -> str | None:
    """Returns the highest resolution image URL with size suffix stripped."""
    if not product_images:
        return None
    highest_key = max((int(k) for k in product_images if k.isdigit()), default=None)
    if highest_key is None:
        return None
    url = product_images.get(str(highest_key))
    if url:
        url = re.sub(r'\._\w+_', '', url)
    return url


def _parse_authors(product: dict, region: str) -> list[dict]:
    """Extracts author name, asin, and region from a product."""
    authors = []
    for author in product.get("authors", []):
        name = author.get("name", "").replace("\t", "").strip()
        asin = author.get("asin", "").replace("\t", "").strip()
        if len(asin) > 12:
            asin = None
        if name:
            authors.append({
                "name": name,
                "asin": asin or None,
                "region": region,
            })
    return authors


def _parse_narrators(product: dict) -> list[str]:
    """Extracts narrator names from a product."""
    return [
        n.get("name", "").strip()
        for n in product.get("narrators", [])
        if n.get("name")
    ]


def _parse_series(product: dict, region: str) -> list[dict]:
    """Extracts series information from a product."""
    series_list = []
    for item in product.get("relationships", []):
        if item.get("relationship_type") == "series":
            series_list.append({
                "asin": item.get("asin"),
                "title": item.get("title"),
                "position": item.get("sequence"),
                "region": region,
            })
    return series_list


def _parse_genres(product: dict) -> list[str]:
    """Extracts genre names from category ladders."""
    genres = []
    for ladder in product.get("category_ladders", []):
        for rung in ladder.get("ladder", []):
            name = rung.get("name")
            if name and name not in genres:
                genres.append(name)
    return genres


def _normalize_product(product: dict, region: str) -> dict[str, Any]:
    """
    Normalizes a raw Audible product into Libex response format.
    Format is compatible with ABM's existing metadata consumer.
    """
    series_list = _parse_series(product, region)

    return {
        "asin": product.get("asin"),
        "title": product.get("title"),
        "subtitle": product.get("subtitle"),
        "authors": _parse_authors(product, region),
        "narrators": _parse_narrators(product),
        "series": series_list,
        "series_name": series_list[0].get("title") if series_list else None,
        "series_asin": series_list[0].get("asin") if series_list else None,
        "series_position": series_list[0].get("position") if series_list else None,
        "series_region": series_list[0].get("region") if series_list else None,
        "cover_url": _best_image(product.get("product_images", {})),
        "description": product.get("merchandising_summary"),
        "summary": product.get("publisher_summary"),
        "publisher": product.get("publisher_name"),
        "language": product.get("language"),
        "runtime_length_min": product.get("runtime_length_min"),
        "rating": product.get("rating", {}).get("overall_distribution", {}).get("average_rating"),
        "genres": _parse_genres(product),
        "release_date": product.get("release_date"),
        "explicit": product.get("is_adult_product", False),
        "has_pdf": product.get("is_pdf_url_available", False),
        "whisper_sync": product.get("read_along_support", False),
        "isbn": product.get("isbn"),
        "content_type": product.get("content_type"),
        "sku": product.get("sku"),
        "region": region,
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
    """
    Fetches a single chunk of up to 50 ASINs from Audible.
    Audible's API hard limit is 50 ASINs per request.
    """
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
    Automatically chunks requests to respect Audible's 50 ASIN limit.
    Falls back to cache only if Audible is unavailable.
    """
    if not asins:
        raise NotFoundException("No ASINs provided")

    seen: set[str] = set()
    unique_asins = [a for a in asins if not (a in seen or seen.add(a))]  # type: ignore

    # Check cache only if explicitly requested
    if use_cache and len(unique_asins) == 1:
        cached = await cache.get(session, book_key(unique_asins[0], region))
        if cached:
            return [cached]

    try:
        chunks = [unique_asins[i:i + 50] for i in range(0, len(unique_asins), 50)]
        all_products = []
        for chunk in chunks:
            products = await _fetch_chunk(chunk, region)
            all_products.extend(products)

        if not all_products:
            raise NotFoundException("No books found for provided ASINs")

        normalized = [_normalize_product(p, region) for p in all_products]

        # Store each book in cache for fallback
        for book in normalized:
            if book.get("asin"):
                await cache.set(session, book_key(book["asin"], region), book)

        logger.info(f"Fetched {len(normalized)} books from Audible", extra={
            "requested": len(unique_asins),
            "returned": len(normalized),
            "region": region,
        })

        return normalized

    except NotFoundException:
        raise

    except Exception:
        # Audible failed — attempt cache fallback
        logger.warning(f"Audible unavailable, attempting cache fallback for {unique_asins}")
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
    """Fetches a single book by ASIN. Convenience wrapper."""
    books = await get_books_by_asins([asin], region, session, use_cache)
    return books[0]


async def get_chapters(
    asin: str,
    region: str,
    session: AsyncSession,
) -> dict[str, Any]:
    """
    Fetches chapter information for a book by ASIN.
    Falls back to cache if Audible is unavailable.
    """
    try:
        path = f"/1.0/content/{asin}/metadata"
        params = {
            "response_groups": "chapter_info, always-returned, content_reference, content_url",
            "quality": "High",
        }

        data = await audible_get(region, path, params)
        chapter_info = data.get("content_metadata", {}).get("chapter_info")

        if not chapter_info:
            raise NotFoundException(f"No chapter information found for {asin}")

        result = {"asin": asin, "region": region, "chapters": chapter_info}
        await cache.set(session, chapters_key(asin, region), result)

        logger.info(f"Fetched chapters for {asin}", extra={"region": region})
        return result

    except NotFoundException:
        raise

    except Exception:
        cached = await cache.get(session, chapters_key(asin, region))
        if cached:
            return cached
        raise NotFoundException("Audible unavailable and no cached chapter data found")