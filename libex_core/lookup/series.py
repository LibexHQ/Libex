"""
Looking a series up: by ASIN its own record and the books in it, and by name
the series whose books match.

Like the book lookups beside it, these are the hosted routes without the
cache. With no store nothing is persisted: an outage is AudibleAPIException,
and Audible having no such series is NotFoundException. With a LocalStore
(keyword store) a series fetched is written through and served as the store
holds it, and an outage is answered from the stored copy, never a confirmed
absence. Nothing here reads the environment.
"""

# Standard library
import logging
import time
from typing import TYPE_CHECKING, Any

# Core
from libex_core.audible.client import (
    AudibleGet,
    as_audible_failure,
    upstream_status_of,
    validate_region,
)
from libex_core.audible.series import fetch_series, fetch_series_book_asins, normalize_series
from libex_core.exceptions import AudibleAPIException, NotFoundException
from libex_core.lookup import _store
from libex_core.lookup._common import OUTAGE_MESSAGE
from libex_core.lookup._shaping import check_shaping
from libex_core.lookup.author_books import BookList, _assemble
from libex_core.lookup.books import Hydration, _canonical_asin, hydrate_books
from libex_core.models import SeriesResponse

if TYPE_CHECKING:
    from libex_core.storage.store import LocalStore

logger = logging.getLogger("libex")

_SERIES_SEARCH_PATH = "/1.0/catalog/products"


async def get_series(
    get: AudibleGet,
    asin: str,
    *,
    region: str = "us",
    store: "LocalStore | None" = None,
) -> SeriesResponse:
    """
    Fetches a series' own record by its ASIN.

    With a store the record is written through and served as the store holds
    it, and when Audible could not be reached the stored record answers.

    Raises NotFoundException (code invalid_request) for a value that is not an
    ASIN, NotFoundException when Audible has no series behind the ASIN,
    AudibleAPIException when Audible could not be reached, and RegionException
    for an unknown region.
    """
    canonical = _canonical_asin(asin)
    region = validate_region(region)
    if store is not None:
        await _store.check(store)
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
        if store is not None:
            stored = await _store.stored_series(store, canonical, region)
            if stored:
                _store.log_served_from_store("series", region, series_asin=canonical)
                return SeriesResponse(**stored)
        raise as_audible_failure(e, OUTAGE_MESSAGE) from e
    if store is not None and await _store.persist_series(store, normalized, region):
        normalized = await _store.stored_series(store, canonical, region) or normalized
    return SeriesResponse(**normalized)


async def get_series_books(
    get: AudibleGet,
    asin: str,
    *,
    region: str = "us",
    filters: dict[str, Any] | None = None,
    sort: str | None = None,
    order: str = "asc",
    store: "LocalStore | None" = None,
) -> BookList:
    """
    Fetches the full books in a series.

    With a store the books are written through and served as the store holds
    them. When Audible could not give the member list, the stored members of
    the series, in series order, answer in its place (the hosted service
    answers that case from its cache of the list, which the library does not
    keep); when it could give the list but not the books, each book is
    answered from its stored copy.

    Reads the series' member ASINs in series order, then hydrates them like a
    bulk lookup. The books come back in the order Audible returned them, or in
    series order when some were answered from the store. A
    member that is not ASIN-shaped, or that Audible has no record of, is left
    out; the bulk lookup is where such ASINs are reported. An empty list is
    possible when none of the members resolves, or when filters leave none.

    As on the hosted route, which sends X-Libex-Complete and
    X-Libex-Incomplete-Reason, the result says whether the list is whole:
    complete is False, with hydration-failed and/or hydration-not-found in
    incomplete_reasons, when a member's request failed or Audible has no record
    of it. The member list is one request, so there is no discovery-incomplete
    here and no deadline, so no hydration-deadline. The one exception is a
    member list Audible could not give and the store answered: the stored
    members may not be the whole series, so discovery-incomplete is reported.
    Judged before filtering. store_write_failed is True when a store was given and
    some fetched book could not be written to it.

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
    if store is not None:
        await _store.check(store)
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
        if store is not None:
            members = await _store.stored_series_books(store, canonical, region)
            if members:
                _store.log_served_from_store(
                    "series books", region, series_asin=canonical, stored_num=len(members)
                )
                # The stored members are whatever the store holds, which
                # nothing confirms is the whole series, so the list is
                # reported as one whose membership was not confirmed.
                return _assemble(
                    False,
                    Hydration(books=members, from_store=[b["asin"] for b in members]),
                    filters,
                    sort,
                    order,
                )
        raise as_audible_failure(e, OUTAGE_MESSAGE) from e

    if not asins:
        raise NotFoundException("No books found for series")
    hydration = await hydrate_books(get, asins, region, store=store)
    return _assemble(True, hydration, filters, sort, order)


async def search_series(
    get: AudibleGet,
    name: str,
    *,
    region: str = "us",
    store: "LocalStore | None" = None,
) -> list[SeriesResponse]:
    """
    Searches for series by name.

    Audible has no series search, so this looks up the first ten catalog
    products titled with the name, takes the series their relationships name
    (each once, in the order found) and fetches each series' own record. A
    series Audible has no record of is left out, and one that could not be
    fetched is left out with a warning, as the hosted route does, since
    the others are still worth returning.

    With a store each series is looked up as get_series does, and the stored
    series whose names match (at most ten) are added after Audible's, those
    Audible already gave left out, as the hosted route does. A search Audible
    could not run at all is an outage, as it is there.

    The name goes to Audible as given and is never logged or repeated in a
    message. Raises NotFoundException when no series was found,
    AudibleAPIException when the search failed, or when series were found
    and every one of them failed to fetch (an empty list there would pass
    an outage off as a confirmed absence), and RegionException for an unknown
    region.
    """
    region = validate_region(region)
    if store is not None:
        await _store.check(store)
    try:
        start = time.monotonic()
        data = await get(region, _SERIES_SEARCH_PATH, {
            "title": name,
            "response_groups": "relationships",
            "num_results": 10,
        })
        products = data.get("products", [])

        seen_asins: set[str] = set()
        series_asins: list[str] = []
        for product in products:
            for rel in product.get("relationships", []):
                if rel.get("relationship_type") == "series":
                    series_asin = rel.get("asin")
                    if series_asin and series_asin not in seen_asins:
                        seen_asins.add(series_asin)
                        series_asins.append(series_asin)

        results: list[SeriesResponse] = []
        skipped_num = 0
        for series_asin in series_asins:
            try:
                results.append(await get_series(get, series_asin, region=region, store=store))
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

        if store is not None:
            for stored in await _store.search_stored_series(store, name):
                if stored.get("asin") and stored["asin"] not in seen_asins:
                    seen_asins.add(stored["asin"])
                    results.append(SeriesResponse(**stored))

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
