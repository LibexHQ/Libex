"""
Audible search service.
Searches the Audible catalog and returns full book metadata.

DESIGN PHILOSOPHY: Audible-first.
Search results are always fresh from Audible.
Cache is used for individual book data fetched after search.
"""

# Standard library
import random
import string
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
from app.services.cache.manager import search_key
from app.services.cache import manager as cache

logger = get_logger()


# ============================================================
# HELPERS
# ============================================================

def _generate_session_id() -> str:
    """Generates a random session ID for Audible requests."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=32))


# ============================================================
# PUBLIC API
# ============================================================

async def search(
    region: str,
    session: AsyncSession,
    title: str | None = None,
    author: str | None = None,
    keywords: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    Searches Audible catalog and returns full book metadata.
    Passes search params directly to Audible's catalog products endpoint.
    Compatible with AudiMeta /search endpoint.
    """
    try:
        params: dict[str, Any] = {
            "num_results": min(limit, 50),
        }

        if title:
            params["title"] = title
        if author:
            params["author"] = author
        if keywords:
            params["keywords"] = keywords

        data = await audible_get(region, "/1.0/catalog/products/", params)
        products = data.get("products", [])

        if not products:
            return []

        asins = [p.get("asin") for p in products if p.get("asin")]

        if not asins:
            return []

        books = await get_books_by_asins(asins, region, session)

        logger.info(f"Search returned {len(books)} results", extra={
            "region": region,
            "params": params,
        })

        return books

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
    """
    Quick search using Audible search suggestions.
    Returns full book metadata for matched ASINs.
    Compatible with AudiMeta /quick-search endpoint.
    """
    try:
        params = {
            "keywords": keywords,
            "key_strokes": keywords,
            "site_variant": "desktop",
            "session_id": _generate_session_id(),
            "local_time": datetime.utcnow().isoformat(),
            "surface": "Android",
        }

        data = await audible_get(region, "/1.0/searchsuggestions", params)
        asins: list[str] = []

        for item in data.get("model", {}).get("items", []):
            if item.get("view", {}).get("template") == "AsinRow":
                asin = item.get("model", {}).get("product_metadata", {}).get("asin")
                if asin:
                    asins.append(asin)

        if not asins:
            return []

        books = await get_books_by_asins(asins, region, session)

        logger.info(f"Quick search returned {len(books)} results", extra={
            "keywords": keywords,
            "region": region,
        })

        return books

    except Exception as e:
        logger.error(f"Quick search failed: {e}")
        return []