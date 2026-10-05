"""
Audible search service.
"""

# Standard library
import time
from typing import Any

# Third party
from sqlalchemy.ext.asyncio import AsyncSession

# Core
from libex_core.audible.books import (
    filter_products,
    normalize_product,
    settle_flags_list,
)
from libex_core.audible.client import as_audible_failure
from libex_core.audible.search import (
    build_search_params,
    fetch_search_products,
    fetch_suggestion_asins,
)
from libex_core.exceptions import AudibleAPIException, NotFoundException
from app.core.logging import get_logger

# Services
from app.services.audible import audible_get
from app.services.audible.books import get_books_by_asins
from app.services.db.persist_queue import persist_books_background
from app.services.db.reader import search_books_from_db

logger = get_logger()


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
    individually -- one API call instead of N+1.
    """
    # Built outside the try on purpose: a limit or page outside what the
    # builder accepts is a bug in the caller's contract, and inside the try
    # it would be reported as an Audible outage.
    params = build_search_params(
        title=title,
        author=author,
        keywords=keywords,
        narrator=narrator,
        publisher=publisher,
        products_sort_by=products_sort_by,
        limit=limit,
        page=page,
    )

    try:
        start = time.monotonic()
        # audible_get is looked up in this module's namespace on every call,
        # never captured, so a stand-in assigned over it is the one called.
        raw_products = await fetch_search_products(audible_get, region, params)
        search_took = round((time.monotonic() - start) * 1000, 2)

        products = filter_products(raw_products)

        # params carries the caller's own title/author/narrator text,
        # so only its keys are logged, never its values. Which fields a
        # consumer searched on is the operational question; what they typed
        # into them is not Libex's to keep.
        logger.info("Requested Audible Search", extra={
            "search_fields": sorted(params),
            "search_took": search_took,
            "region": region,
            "results": len(products),
        })

        if not products:
            return []

        # Normalize directly from search results -- no re-fetch needed
        normalized = [normalize_product(p, region) for p in products]

        # Persist to DB and cache in the background. Unsettled: the writer
        # needs the tri-state flags None/True/False as normalize_product
        # produced them (see libex_core.storage.write.support.asserted_bool), so this runs
        # before settle_flags_list below, on the pre-settle list.
        persist_books_background(normalized, region)

        # filter_dicts (app/services/filtering.py) and BookResponse both run
        # on what this returns and neither tolerates a None flag -- settle
        # here, after the persist above got the tri-state it needs.
        return settle_flags_list(normalized)

    except NotFoundException:
        return []
    except Exception as e:
        logger.error("Search failed", extra={"region": region, "error": str(e)})
        # Whatever this was, it is not Audible answering that nothing
        # matched -- an empty list here would be indistinguishable from a
        # genuine zero-result search, which is exactly the ambiguity a
        # caller must not be handed.
        raise as_audible_failure(e, "Audible search failed") from e


async def quick_search(
    keywords: str,
    region: str,
    session: AsyncSession,
    use_cache: bool = False,
) -> list[dict[str, Any]]:
    """Quick search using Audible search suggestions.

    Falls back to catalog search if the keywords look like a compound
    ABS-style query (e.g. "Author - Series - Title") and suggestions
    return nothing. Falls back to the local DB if catalog also returns
    nothing.

    use_cache is passed straight through to the ASIN hydration below and
    nowhere else in this function -- quick_search has an ASIN list only
    after the suggestions call, so that hydration is the one place here a
    cached book can save an Audible fetch at all. search() below has no
    comparable ASIN list to key a cache read on and stays uncached.
    """
    try:
        start = time.monotonic()
        asins = await fetch_suggestion_asins(audible_get, keywords, region)
        search_took = round((time.monotonic() - start) * 1000, 2)

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
            return await get_books_by_asins(asins, region, session, use_cache)

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

                try:
                    catalog_results = await search(
                        region=region,
                        session=session,
                        title=parsed_title,
                        author=parsed_author,
                        limit=10,
                    )
                except AudibleAPIException:
                    # A transport failure inside this one fallback avenue is
                    # not the end of the request -- the local DB is a third,
                    # independent source for a compound query, so it still
                    # gets tried below exactly as it does on a genuine
                    # zero-result catalog search.
                    catalog_results = []
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

    except NotFoundException:
        return []
    except Exception as e:
        logger.error("Quick search failed", extra={"region": region, "error": str(e)})
        # Whatever this was, it is not Audible answering that nothing
        # matched -- an empty list here would be indistinguishable from a
        # genuine zero-result search, which is exactly the ambiguity a
        # caller must not be handed. Only the catalog leg of the
        # compound-query fallback gets its own narrower catch above, so it
        # can still hand off to the local DB; a failure of the suggestions
        # lookup itself, or of hydrating the ASINs suggestions returned, has
        # no further fallback left and reaches here directly.
        raise as_audible_failure(e, "Audible quick search failed") from e
