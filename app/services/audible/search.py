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
from app.services.audible.books import (
    get_books_by_asins,
    _normalize_product,
    _filter_products,
    _settle_flags_list,
    BOOK_RESPONSE_GROUPS,
    IMAGE_SIZES,
)
from app.services.db.persist_queue import persist_books_background
from app.services.db.reader import search_books_from_db

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

    Includes response_groups in the search call so Audible returns full
    product metadata directly. This avoids re-fetching each book
    individually — one API call instead of N+1.
    """
    try:
        params: dict[str, Any] = {
            "num_results": min(limit, 50),
            "page": page,
            "response_groups": BOOK_RESPONSE_GROUPS,
            "image_sizes": IMAGE_SIZES,
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

        search_params = {k: v for k, v in params.items()
                         if k not in ("response_groups", "image_sizes")}

        start = time.monotonic()
        data = await audible_get(region, "/1.0/catalog/products/", params)
        search_took = round((time.monotonic() - start) * 1000, 2)

        products = _filter_products(data.get("products", []))

        # search_params carries the caller's own title/author/narrator text,
        # so only its keys are logged, never its values. Which fields a
        # consumer searched on is the operational question; what they typed
        # into them is not Libex's to keep.
        logger.info("Requested Audible Search", extra={
            "search_fields": sorted(search_params),
            "search_took": search_took,
            "region": region,
            "results": len(products),
        })

        if not products:
            return []

        # Normalize directly from search results — no re-fetch needed
        normalized = [_normalize_product(p, region) for p in products]

        # Persist to DB and cache in the background. Unsettled: the writer
        # needs the tri-state flags None/True/False as _normalize_product
        # produced them (see _asserted_bool in writer.py), so this runs
        # before _settle_flags_list below, on the pre-settle list.
        persist_books_background(normalized, region)

        # filter_dicts (app/services/filtering.py) and BookResponse both run
        # on what this returns and neither tolerates a None flag -- settle
        # here, after the persist above got the tri-state it needs.
        return _settle_flags_list(normalized)

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
    """Quick search using Audible search suggestions.

    Falls back to catalog search if the keywords look like a compound
    ABS-style query (e.g. "Author - Series - Title") and suggestions
    return nothing. Falls back to the local DB if catalog also returns
    nothing.
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

        start = time.monotonic()
        data = await audible_get(region, "/1.0/searchsuggestions", params)
        search_took = round((time.monotonic() - start) * 1000, 2)

        asins: list[str] = []

        for item in data.get("model", {}).get("items", []):
            if item.get("view", {}).get("template") == "AsinRow":
                asin = item.get("model", {}).get("product_metadata", {}).get("asin")
                if asin:
                    asins.append(asin)

        # Deliberately no "keywords" field. It is verbatim caller-authored
        # text, and Libex records nothing that identifies a caller and nothing
        # a caller typed. The length is kept instead: it is enough to
        # correlate a slow or empty search with the size of the query that
        # produced it, without keeping the query.
        logger.info("Requested Audible Quick Search", extra={
            "keywords_length": len(keywords),
            "search_took": search_took,
            "region": region,
            "suggestions_found": len(asins),
        })

        if asins:
            return await get_books_by_asins(asins, region, session)

        # Suggestions returned nothing — check for compound ABS-style query
        # Format: "Author - Series - Title" or "Author - Title"
        if " - " in keywords:
            segments = [s.strip() for s in keywords.split(" - ") if s.strip()]
            if len(segments) >= 2:
                parsed_author = segments[0]
                parsed_title = segments[-1]

                # Same rule as above: keywords, parsed_author and
                # parsed_title are all the caller's own words and none of them
                # is logged. What matters operationally is that this fallback
                # fired at all and how the parser split the query, so the
                # segment count is kept and the segments are not.
                logger.info("Quick search compound fallback", extra={
                    "keywords_length": len(keywords),
                    "segments_found": len(segments),
                    "region": region,
                })

                catalog_results = await search(
                    region=region,
                    session=session,
                    title=parsed_title,
                    author=parsed_author,
                    limit=10,
                )
                if catalog_results:
                    return catalog_results

                # Catalog also returned nothing — try local DB
                db_results = await search_books_from_db(
                    session=session,
                    title=parsed_title,
                    author_name=parsed_author,
                    limit=10,
                )
                if db_results:
                    return db_results

        return []

    except Exception as e:
        logger.error(f"Quick search failed: {e}")
        return []