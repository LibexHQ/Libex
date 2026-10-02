"""
Looking a series up by ASIN: its own record, and the books in it.

Like the book lookups beside it, these are the hosted routes' live path
without the cache, the database backstop or persistence: an outage is
AudibleAPIException, and Audible having no such series is NotFoundException.
Nothing here reads the environment.
"""

# Standard library
import logging
import time

# Core
from libex_core.audible.client import (
    AudibleGet,
    as_audible_failure,
    upstream_status_of,
    validate_region,
)
from libex_core.audible.series import fetch_series, fetch_series_book_asins, normalize_series
from libex_core.exceptions import NotFoundException
from libex_core.lookup.books import _OUTAGE_MESSAGE, _canonical_asin, hydrate_books
from libex_core.models import BookResponse, SeriesResponse

logger = logging.getLogger("libex")


async def get_series(get: AudibleGet, asin: str, *, region: str = "us") -> SeriesResponse:
    """
    Fetches a series' own record by its ASIN.

    Raises NotFoundException (code invalid_request) for a value that is not an
    ASIN, NotFoundException when Audible has no series behind the ASIN,
    AudibleAPIException when Audible could not be reached, and RegionException
    for an unknown region.
    """
    canonical = _canonical_asin(asin)
    region = validate_region(region)
    try:
        start = time.monotonic()
        product = await fetch_series(get, canonical, region)
        series_took = round((time.monotonic() - start) * 1000, 2)

        normalized = normalize_series(product, region)

        logger.info("Requested Audible Series", extra={
            "series_took": series_took,
            "region": region,
        })
    except NotFoundException:
        raise
    except Exception as e:
        logger.warning("Audible unavailable for series", extra={
            "series_asin": canonical,
            "region": region,
            "error_type": type(e).__name__,
            "upstream_status": upstream_status_of(e),
        })
        raise as_audible_failure(e, _OUTAGE_MESSAGE) from e
    return SeriesResponse(**normalized)


async def get_series_books(
    get: AudibleGet, asin: str, *, region: str = "us"
) -> list[BookResponse]:
    """
    Fetches the full books in a series.

    Reads the series' member ASINs in series order, then hydrates them like a
    bulk lookup. The books come back in the order Audible returned them. A
    member that is not ASIN-shaped, or that Audible has no record of, is left
    out; the bulk lookup is where such ASINs are reported. An empty list is
    possible when none of the members resolves.

    Raises NotFoundException (code invalid_request) for a value that is not an
    ASIN, NotFoundException when the series has no members, AudibleAPIException
    when Audible could not be reached (for the member list, or for every book),
    and RegionException for an unknown region.
    """
    canonical = _canonical_asin(asin)
    region = validate_region(region)
    try:
        start = time.monotonic()
        asins = await fetch_series_book_asins(get, canonical, region)
        series_book_took = round((time.monotonic() - start) * 1000, 2)

        logger.info("Requested Audible Series Books", extra={
            "series_book_num": len(asins),
            "series_book_took": series_book_took,
            "region": region,
        })
    except NotFoundException:
        raise
    except Exception as e:
        logger.warning("Audible unavailable for series books", extra={
            "series_asin": canonical,
            "region": region,
            "error_type": type(e).__name__,
            "upstream_status": upstream_status_of(e),
        })
        raise as_audible_failure(e, _OUTAGE_MESSAGE) from e

    hydration = await hydrate_books(get, asins, region)
    return [BookResponse(**book) for book in hydration.books]
