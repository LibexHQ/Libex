"""
Audible search service.
"""

# Standard library
import time
from datetime import datetime
from typing import Any

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Core
from app.core.exceptions import NotFoundException
from app.core.logging import get_logger

# Services
from app.services.audible.client import audible_get
from app.services.audible.books import get_books_by_asins

logger = get_logger()


def _generate_session_id() -> str:
    """
    Generates a random session ID matching AudiMeta's format.
    Format: 000-XXXXXXX-XXXXXXX
    """
    import random
    def random_digits() -> str:
        return str(random.randint(0, 9999999)).zfill(7)
    return f"000-{random_digits()}-{random_digits()}"


async def search(
    region: str,
    session: AsyncSession,
    title: str | None = None,
    author: str | None = None,
    keywords: str | None = None,
    limit: int = 10,
    narrator: str | None = None,
    publisher: str | None = None,
    products_sort_by: str | None = None,
    page: int = 0,
) -> list[dict[str, Any]]:
    """
    Searches Audible catalog and returns full book metadata.
    Passes all search params directly to Audible matching AudiMeta's behavior.
    """
    try:
        params: dict[str, Any] = {
            "num_results": min(limit, 50),
            "page": page,
        }

        if title:
            params["title"] = title
        if author:
            params["author"] = author
        if keywords:
            params["keywords"] = keywords
        if narrator:
            params["narrator"] = narrator
        if publisher:
            params["publisher"] = publisher
        if products_sort_by:
            params["products_sort_by"] = products_sort_by

        search_params = {k: v for k, v in params.items()}

        start = time.monotonic()
        data = await audible_get(region, "/1.0/catalog/products/", params)
        search_took = round((time.monotonic() - start) * 1000, 2)

        products = data.get("products", [])

        logger.info("Requested Audible Search", extra={
            "search_params": search_params,
            "search_took": search_took,
            "region": region,
        })

        if not products:
            return []

        asins = [p.get("asin") for p in products if p.get("asin")]
        if not asins:
            return []

        return await get_books_by_asins(asins, region, session)

    except NotFoundException:
        return []
    except Exception as e:
        logger.error(f"Search failed: {e}")
        return []


async def quick_search(
    keywords: str,
    region: str,
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Quick search using Audible search suggestions."""
    try:
        params = {
            "keywords": keywords,
            "key_strokes": keywords,
            "site_variant": "desktop",
            "session_id": _generate_session_id(),
            "local_time": datetime.utcnow().isoformat(),
            "surface": "Android",
        }

        start = time.monotonic()
        data = await audible_get(region, "/1.0/searchsuggestions", params)
        search_took = round((time.monotonic() - start) * 1000, 2)

        asins: list[str] = []

        for item in data.get("model", {}).get("items", []):
            if item.get("view", {}).get("template") == "AsinRow":
                asin = item.get("model", {}).get("product_metadata", {}).get("asin")
                if asin:
                    asins.append(asin)

        logger.info("Requested Audible Quick Search", extra={
            "search_took": search_took,
            "region": region,
        })

        if not asins:
            return []

        return await get_books_by_asins(asins, region, session)

    except Exception as e:
        logger.error(f"Quick search failed: {e}")
        return []