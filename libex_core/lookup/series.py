"""
Looking a series up: by ASIN its own record and the books in it, and by name
the series whose books match.

Like the book lookups beside it, these are the hosted routes' live path
without the cache, the database backstop or persistence: an outage is
AudibleAPIException, and Audible having no such series is NotFoundException.
Nothing here reads the environment.
"""

# Standard library
import logging
import time
from typing import Any

# Core
from libex_core.audible.client import (
    AudibleGet,
    as_audible_failure,
    upstream_status_of,
    validate_region,
)
from libex_core.audible.series import fetch_series, fetch_series_book_asins, normalize_series
from libex_core.exceptions import AudibleAPIException, NotFoundException
from libex_core.lookup._common import OUTAGE_MESSAGE
from libex_core.lookup._shaping import check_shaping
from libex_core.lookup.author_books import BookList, _assemble
from libex_core.lookup.books import _canonical_asin, hydrate_books
from libex_core.models import SeriesResponse

logger = logging.getLogger("libex")

_SERIES_SEARCH_PATH = "/1.0/catalog/products"


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
        raise as_audible_failure(e, OUTAGE_MESSAGE) from e
    return SeriesResponse(**normalized)


async def get_series_books(
    get: AudibleGet,
    asin: str,
    *,
    region: str = "us",
    filters: dict[str, Any] | None = None,
    sort: str | None = None,
    order: str = "asc",
) -> BookList:
    """
    Fetches the full books in a series.

    Reads the series' member ASINs in series order, then hydrates them like a
    bulk lookup. The books come back in the order Audible returned them. A
    member that is not ASIN-shaped, or that Audible has no record of, is left
    out; the bulk lookup is where such ASINs are reported. An empty list is
    possible when none of the members resolves, or when filters leave none.

    As on the hosted route, which sends X-Libex-Complete and
    X-Libex-Incomplete-Reason, the result says whether the list is whole:
    complete is False, with hydration-failed and/or hydration-not-found in
    incomplete_reasons, when a member's request failed or Audible has no record
    of it. The member list is one request, so there is no discovery-incomplete
    here and no deadline, so no hydration-deadline. Judged before filtering.

    filters, sort and order are applied as on the hosted route: the books keep
    series order unless a sort is given, which overrides it. See
    libex_core.shaping for the filter names and sortable fields; a name or
    value outside them is ValueError, before anything is sent.

    Raises NotFoundException (code invalid_request) for a value that is not an
    ASIN, NotFoundException when the series has no members, AudibleAPIException
    when Audible could not be reached (for the member list, or for every book),
    and RegionException for an unknown region.
    """
    canonical = _canonical_asin(asin)
    region = validate_region(region)
    check_shaping(filters, sort, order)
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
        raise as_audible_failure(e, OUTAGE_MESSAGE) from e

    if not asins:
        raise NotFoundException("No books found for series")
    hydration = await hydrate_books(get, asins, region)
    return _assemble(True, hydration, filters, sort, order)


async def search_series(
    get: AudibleGet, name: str, *, region: str = "us"
) -> list[SeriesResponse]:
    """
    Searches for series by name.

    Audible has no series search, so this looks up the first ten catalog
    products titled with the name, takes the series their relationships name
    (each once, in the order found) and fetches each series' own record. A
    series Audible has no record of is left out, and one that could not be
    fetched is left out with a warning, as the hosted route does, since
    the others are still worth returning.

    The name goes to Audible as given and is never logged or repeated in a
    message. Raises NotFoundException when no series was found,
    AudibleAPIException when the search failed, or when series were found
    and every one of them failed to fetch (an empty list there would pass
    an outage off as a confirmed absence), and RegionException for an unknown
    region.
    """
    region = validate_region(region)
    try:
        start = time.monotonic()
        data = await get(region, _SERIES_SEARCH_PATH, {
            "title": name,
            "response_groups": "relationships",
            "num_results": 10,
        })
        products = data.get("products") or []

        seen_asins: set[str] = set()
        series_asins: list[str] = []
        for product in products:
            for rel in product.get("relationships") or []:
                if rel.get("relationship_type") == "series":
                    series_asin = rel.get("asin")
                    if series_asin and series_asin not in seen_asins:
                        seen_asins.add(series_asin)
                        series_asins.append(series_asin)

        results: list[SeriesResponse] = []
        skipped_num = 0
        for series_asin in series_asins:
            try:
                results.append(await get_series(get, series_asin, region=region))
            except NotFoundException:
                continue
            except AudibleAPIException:
                # get_series already logged the failure; one warning for the
                # lot follows the loop.
                skipped_num += 1
                continue

        if skipped_num:
            logger.warning(
                "Series search: could not resolve one or more related series, skipping",
                extra={"region": region, "skipped_num": skipped_num},
            )

        search_took = round((time.monotonic() - start) * 1000, 2)

        if not results:
            if skipped_num:
                raise AudibleAPIException("Series search failed")
            raise NotFoundException("No series found")

        logger.info("Searched Audible for series", extra={
            "series_result_num": len(results),
            "search_took": search_took,
            "region": region,
        })
        return results

    except NotFoundException:
        raise
    except Exception as e:
        logger.warning("Series search failed", extra={
            "name_length": len(name),
            "region": region,
            "error_type": type(e).__name__,
            "upstream_status": upstream_status_of(e),
        })
        raise as_audible_failure(e, "Series search failed") from e
