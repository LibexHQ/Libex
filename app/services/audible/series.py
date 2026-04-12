"""
Audible series service.
Fetches series metadata directly from the Audible API.

DESIGN PHILOSOPHY: Audible-first.
Always fetches fresh data from Audible.
Cache is used only as a fallback when Audible is unavailable.
"""

# Standard library
from typing import Any
import time

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Core
from app.core.exceptions import NotFoundException
from app.core.logging import get_logger
from app.core.utils import strip_html

# Services
from app.services.audible.client import audible_get
from app.services.cache import manager as cache
from app.services.cache.manager import series_key, series_books_key

logger = get_logger()

SERIES_RESPONSE_GROUPS = "product_attrs, product_desc, product_extended_attrs"
SERIES_BOOKS_RESPONSE_GROUPS = "relationships"


# ============================================================
# HELPERS
# ============================================================

def _normalize_series(product: dict, region: str) -> dict[str, Any]:
    """Normalizes raw Audible product data into Libex series format."""
    return {
        "asin": product.get("asin"),
        "name": product.get("title"),
        "description": strip_html(product.get("publisher_summary")),
        "region": region,
        "position": None,
        "updatedAt": None,
    }


# ============================================================
# PUBLIC API
# ============================================================

async def get_series(
    asin: str,
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
) -> dict[str, Any]:
    """
    Fetches series metadata by ASIN.
    Audible-first with cache fallback.
    """
    if use_cache:
        cached = await cache.get(session, series_key(asin, region))
        if cached:
            return cached

    try:
        path = f"/1.0/catalog/products/{asin}"
        params = {
            "response_groups": SERIES_RESPONSE_GROUPS,
        }
        data = await audible_get(region, path, params)

        if (
            not data
            or not data.get("response_groups")
            or len(data.get("response_groups", [])) == 1
        ):
            raise NotFoundException(f"Series not found: {asin}")

        product = data.get("product")
        if not product:
            raise NotFoundException(f"Series not found: {asin}")

        normalized = _normalize_series(product, region)
        await cache.set(session, series_key(asin, region), normalized)

        logger.info(f"Fetched series {asin}", extra={"region": region})
        return normalized

    except NotFoundException:
        raise

    except Exception:
        cached = await cache.get(session, series_key(asin, region))
        if cached:
            return cached
        raise NotFoundException("Audible unavailable and no cached series data found")


async def get_series_books(
    asin: str,
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
) -> list[str]:
    """
    Fetches all book ASINs for a series, sorted by position.
    Uses relationships response group from the series product endpoint.
    """
    if use_cache:
        cached = await cache.get(session, series_books_key(asin, region))
        if cached:
            return cached

    try:
        path = f"/1.0/catalog/products/{asin}"
        params = {
            "response_groups": SERIES_BOOKS_RESPONSE_GROUPS,
        }
        data = await audible_get(region, path, params)

        product = data.get("product", {})
        relationships = product.get("relationships", [])

        items = sorted(
            [r for r in relationships if r.get("asin") and r.get("sort")],
            key=lambda r: float(r.get("sort", 0)),
        )

        asins = [item["asin"] for item in items]

        if not asins:
            raise NotFoundException(f"No books found for series: {asin}")

        await cache.set(session, series_books_key(asin, region), asins)

        logger.info(f"Fetched {len(asins)} books for series {asin}", extra={"region": region})
        return asins

    except NotFoundException:
        raise

    except Exception:
        cached = await cache.get(session, series_books_key(asin, region))
        if cached:
            return cached
        raise NotFoundException("Audible unavailable and no cached series books found")


async def search_series(
    name: str,
    region: str,
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """
    Searches for series by name.
    Searches Audible products by title, extracts unique series from relationships,
    then fetches each series by ASIN. Returns a deduplicated list sorted by relevance.
    Audible-first: when DB is available this can be augmented with local results.
    """
    try:
        start = time.monotonic()

        # Step 1: Search Audible products by title to find books in matching series
        path = "/1.0/catalog/products"
        params = {
            "title": name,
            "response_groups": "relationships",
            "num_results": 10,
        }
        data = await audible_get(region, path, params)
        products = data.get("products", [])

        if not products:
            raise NotFoundException(f"No series found for: {name}")

        # Step 2: Extract unique series ASINs from product relationships
        seen_asins: set[str] = set()
        series_asins: list[str] = []
        for product in products:
            for rel in product.get("relationships", []):
                if rel.get("relationship_type") == "series":
                    asin = rel.get("asin")
                    if asin and asin not in seen_asins:
                        seen_asins.add(asin)
                        series_asins.append(asin)

        if not series_asins:
            raise NotFoundException(f"No series found for: {name}")

        # Step 3: Fetch full series metadata for each unique series ASIN
        results = []
        for asin in series_asins:
            try:
                series = await get_series(asin, region, session)
                results.append(series)
            except NotFoundException:
                continue

        search_took = round((time.monotonic() - start) * 1000, 2)

        if not results:
            raise NotFoundException(f"No series found for: {name}")

        logger.info("Searched Audible for series", extra={
            "series_result_num": len(results),
            "search_took": search_took,
            "region": region,
        })

        return results

    except NotFoundException:
        raise
    except Exception:
        raise NotFoundException("Series search failed")