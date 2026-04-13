"""
Audible series service.
Fetches series metadata directly from the Audible API.

DESIGN PHILOSOPHY: Audible-first.
Always fetches fresh data from Audible.
Writes every result to the relational DB for persistence.
Falls back to DB when Audible is unavailable.
"""

# Standard library
import time
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
from app.services.db.writer import upsert_series_profile
from app.services.db.reader import get_series_from_db, search_series_from_db

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
    Audible-first, writes to DB, falls back to DB then cache.
    """
    if use_cache:
        cached = await cache.get(session, series_key(asin, region))
        if cached:
            return cached

    try:
        start = time.monotonic()
        path = f"/1.0/catalog/products/{asin}"
        params = {
            "response_groups": SERIES_RESPONSE_GROUPS,
        }
        data = await audible_get(region, path, params)
        series_took = round((time.monotonic() - start) * 1000, 2)

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

        # Write to DB and cache
        await upsert_series_profile(session, normalized)
        await cache.set(session, series_key(asin, region), normalized)

        logger.info("Requested Audible Series", extra={
            "series_took": series_took,
            "region": region,
        })

        return normalized

    except NotFoundException:
        raise

    except Exception:
        # Try DB first
        db_result = await get_series_from_db(session, asin)
        if db_result:
            return db_result

        # Fall back to cache
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
        start = time.monotonic()
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
        series_book_took = round((time.monotonic() - start) * 1000, 2)

        if not asins:
            raise NotFoundException(f"No books found for series: {asin}")

        await cache.set(session, series_books_key(asin, region), asins)

        logger.info("Requested Audible Series Books", extra={
            "series_book_num": len(asins),
            "series_book_took": series_book_took,
            "region": region,
        })

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
    Step 1: Search Audible products by title to find books in matching series.
    Step 2: Extract unique series ASINs from relationships.
    Step 3: Fetch full series metadata for each ASIN.
    Also checks the local DB for additional matches.
    Results are deduplicated with Audible results taking priority.
    """
    try:
        start = time.monotonic()

        # Step 1: Search Audible products by title
        path = "/1.0/catalog/products"
        params = {
            "title": name,
            "response_groups": "relationships",
            "response_groups": "relationships",
            "num_results": 10,
        }
        data = await audible_get(region, path, params)
        products = data.get("products", [])

        # Step 2: Extract unique series ASINs from relationships
        seen_asins: set[str] = set()
        series_asins: list[str] = []

        for product in products:
            for rel in product.get("relationships", []):
                if rel.get("relationship_type") == "series":
                    asin = rel.get("asin")
                    if asin and asin not in seen_asins:
                        seen_asins.add(asin)
                        series_asins.append(asin)

        # Step 3: Fetch full series metadata
        results = []
        for asin in series_asins:
            try:
                series = await get_series(asin, region, session)
                results.append(series)
            except NotFoundException:
                continue

        # Also check DB for additional matches not found via Audible
        db_results = await search_series_from_db(session, name)
        for db_series in db_results:
            db_asin = db_series.get("asin")
            if db_asin and db_asin not in seen_asins:
                seen_asins.add(db_asin)
                results.append(db_series)

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