"""
Audible authors service.
Fetches author metadata directly from the Audible API.

DESIGN PHILOSOPHY: Audible-first.
Always fetches fresh data from Audible.
Cache is used only as a fallback when Audible is unavailable.
"""

# Standard library
from typing import Any
import random
import string

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Core
from app.core.exceptions import NotFoundException
from app.core.logging import get_logger

# Services
from app.services.audible.client import audible_get, get_audible_url, get_region_headers, LOCALE_MAP
from app.services.cache import manager as cache
from app.services.cache.manager import author_key, author_books_key

logger = get_logger()


# ============================================================
# HELPERS
# ============================================================

def _generate_session_id() -> str:
    """Generates a random session ID for Android app endpoint requests."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=32))


def _normalize_author(data: dict, asin: str, region: str) -> dict[str, Any]:
    contributor = data.get("contributor", {})
    return {
        "asin": asin,
        "name": contributor.get("name", "").replace("\t", "").strip(),
        "description": contributor.get("bio", "").replace("\t", "").strip() or None,
        "image": contributor.get("profile_image_url"),
        "region": region,
    }


# ============================================================
# AUDIBLE REQUESTS
# ============================================================

async def _fetch_author_details(asin: str, region: str) -> dict[str, Any]:
    """
    Fetches author profile from Audible contributors endpoint.
    Returns bio, image, and name.
    Credits: https://github.com/sunbrolynk
    """
    path = f"/1.0/catalog/contributors/{asin}"
    params = {
        "locale": LOCALE_MAP.get(region, "en-US"),
    }
    return await audible_get(region, path, params)


async def _fetch_author_books_page(
    asin: str,
    region: str,
    token: str | None = None,
) -> dict[str, Any]:
    """
    Fetches a page of author books using the Audible Android app endpoint.
    Uses continuation tokens for pagination rather than page numbers.
    """
    path = f"/1.0/screens/audible-android-author-detail/{asin}"
    params: dict[str, Any] = {
        "tabId": "titles",
        "author_asin": asin,
        "title_source": "all",
        "session_id": _generate_session_id(),
        "applicationType": "Android_App",
        "local_time": __import__("datetime").datetime.utcnow().isoformat(),
        "response_groups": "always-returned",
        "surface": "Android",
    }
    if token:
        params["pageSectionContinuationToken"] = token

    return await audible_get(region, path, params)


async def _fetch_author_books_by_name(name: str, region: str) -> list[str]:
    asins: list[str] = []
    page = 0

    while page <= 20:
        path = "/1.0/catalog/products"
        params = {
            "author": name,
            "num_results": 50,
            "page": page,
            "response_groups": "product_desc,contributors,series,product_attrs,media",
            "sort_by": "-ReleaseDate",
        }
        data = await audible_get(region, path, params)
        products = data.get("products", [])

        if not products:
            break

        for product in products:
            # Match by author name to filter false positives
            matches = any(
                a.get("name", "").lower() == name.lower()
                for a in product.get("authors", [])
            )
            asin = product.get("asin")
            if asin and matches and asin not in asins:
                asins.append(asin)

        if len(products) < 50:
            break

        page += 1

    return asins


# ============================================================
# PUBLIC API
# ============================================================

async def get_author(
    asin: str,
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
) -> dict[str, Any]:
    """
    Fetches author profile by ASIN.
    Audible-first with cache fallback.
    """
    if use_cache:
        cached = await cache.get(session, author_key(asin, region))
        if cached:
            return cached

    try:
        data = await _fetch_author_details(asin, region)

        if not data or data.get("contributor", {}).get("name") is None:
            raise NotFoundException(f"Author not found: {asin}")

        normalized = _normalize_author(data, asin, region)
        await cache.set(session, author_key(asin, region), normalized)

        logger.info(f"Fetched author {asin}", extra={"region": region})
        return normalized

    except NotFoundException:
        raise

    except Exception:
        cached = await cache.get(session, author_key(asin, region))
        if cached:
            return cached
        raise NotFoundException("Audible unavailable and no cached author data found")


async def get_author_books(
    asin: str,
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
) -> list[str]:
    """
    Fetches all book ASINs for an author using the Android endpoint.
    Uses continuation token pagination.
    Returns list of ASINs for the caller to fetch full book data.
    """
    if use_cache:
        cached = await cache.get(session, author_books_key(asin, region))
        if cached:
            return cached

    try:
        asins: list[str] = []
        pagination_token: str | None = None
        first_run = True
        page = 0

        while (first_run or pagination_token) and page <= 10:
            first_run = False
            data = await _fetch_author_books_page(asin, region, pagination_token)

            for section in data.get("sections", []):
                if section.get("model", {}).get("rows") and section.get("pagination") is not None:
                    for item in section["model"]["rows"]:
                        meta = item.get("product_metadata", {})
                        if meta.get("asin"):
                            asins.append(meta["asin"])
                    pagination_token = section.get("pagination")
                    break

            page += 1

        if not asins:
            raise NotFoundException(f"No books found for author: {asin}")

        await cache.set(session, author_books_key(asin, region), asins)
        logger.info(f"Fetched {len(asins)} book ASINs for author {asin}")
        return asins

    except NotFoundException:
        raise

    except Exception:
        cached = await cache.get(session, author_books_key(asin, region))
        if cached:
            return cached
        raise NotFoundException("Audible unavailable and no cached author books found")


async def get_author_books_by_name(
    name: str,
    region: str,
    session: AsyncSession,
) -> list[str]:
    """
    Fetches book ASINs by author name.
    Used when no ASIN is available.
    """
    try:
        asins = await _fetch_author_books_by_name(name, region)
        if not asins:
            raise NotFoundException(f"No books found for author name: {name}")
        return asins
    except NotFoundException:
        raise
    except Exception:
        raise NotFoundException("Failed to fetch author books by name")


async def search_authors(
    name: str,
    region: str,
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """
    Searches for authors by name using Audible search suggestions.
    Returns list of author profiles.
    """
    try:
        path = "/1.0/searchsuggestions"
        params = {
            "keywords": name,
            "key_strokes": name,
            "site_variant": "android-mshop",
            "session_id": _generate_session_id(),
            "local_time": __import__("datetime").datetime.utcnow().isoformat(),
            "surface": "Android",
        }

        data = await audible_get(region, path, params)
        asins: list[str] = []

        for item in data.get("model", {}).get("items", []):
            if item.get("view", {}).get("template") == "AuthorItemV2":
                asin = item.get("model", {}).get("person_metadata", {}).get("asin")
                if asin:
                    asins.append(asin)

        if not asins:
            return []

        authors = []
        for asin in asins:
            try:
                author = await get_author(asin, region, session)
                authors.append(author)
            except NotFoundException:
                continue

        logger.info(f"Found {len(authors)} authors for query: {name}")
        return authors

    except NotFoundException:
        raise
    except Exception:
        raise NotFoundException("Author search failed")