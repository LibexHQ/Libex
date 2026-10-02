"""
Searching Audible: the catalog search, the suggestions-backed quick search,
their Audiobookshelf-shaped twins, and the books of a narrator.

The library's counterparts of the hosted routes, without the cache. A search
that matches nothing is NotFoundException, as on the hosted routes, and a
search Audible could not answer is AudibleAPIException: an outage is never
reported as an empty result.

With a LocalStore (keyword store) the books a search returns are written
through and each is served as the store then holds it. As on the hosted
service, a catalog search that Audible cannot answer is not answered from the
store; the one place the store stands in is the quick search's compound
"Author - Title" leg, and the hydration of suggested ASINs, which falls back
book by book exactly as the book lookups do.

What a caller typed is sent to Audible as given and never inspected, logged or
repeated in an exception message. Nothing here reads the environment.
"""

# Standard library
import logging
import time
from typing import TYPE_CHECKING, Any

# Core
from libex_core.audible.books import filter_products, normalize_product, settle_flags_list
from libex_core.audible.client import (
    AudibleGet,
    as_audible_failure,
    validate_region,
)
from libex_core.audible.search import (
    build_search_params,
    fetch_search_products,
    fetch_suggestion_asins,
)
from libex_core.exceptions import (
    AudibleAPIException,
    ErrorCode,
    NotFoundException,
    RegionException,
)
from libex_core.lookup import _store
from libex_core.lookup.books import hydrate_books
from libex_core.models import AbsSearchResponse, BookResponse, to_abs_book

if TYPE_CHECKING:
    from libex_core.storage.store import LocalStore

logger = logging.getLogger("libex")

# Audible's catalog search returns a full page under HTTP 200 past roughly its
# tenth page rather than an error or an empty list, so a caller paging until
# empty would never stop. Stopping at 9 is deliberately conservative, not the
# measured edge; the hosted routes enforce the same bound.
MAX_SEARCH_PAGE = 9

# The number of results the Audiobookshelf-shaped search asks for.
ABS_SEARCH_LIMIT = 5

_NOT_FOUND_MESSAGE = "No books found"


# ============================================================
# INTERNAL
# ============================================================

async def _search_books(
    get: AudibleGet,
    region: str,
    *,
    title: str | None = None,
    author: str | None = None,
    keywords: str | None = None,
    narrator: str | None = None,
    publisher: str | None = None,
    products_sort_by: str | None = None,
    limit: int = 10,
    page: int = 0,
    store: "LocalStore | None" = None,
) -> list[dict[str, Any]]:
    """
    Runs one catalog search and returns settled book dicts; empty when Audible
    answered that nothing matched. With a store the books are written through
    first and each is served as the store then holds it.

    The region and the paging bounds are checked outside the try on purpose: a
    value outside what they accept is the caller's mistake, and inside the try
    it would be reported as an Audible outage. Raises ValueError for a limit
    outside 1..50 or a page outside 0..MAX_SEARCH_PAGE (the message names the
    bounds, never a search value), RegionException for an unknown region, and
    AudibleAPIException when the search failed.
    """
    region = validate_region(region)
    if page > MAX_SEARCH_PAGE:
        raise ValueError(f"page must be at most {MAX_SEARCH_PAGE}")
    if store is not None:
        await _store.check(store)
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
        raw_products = await fetch_search_products(get, region, params)
        search_took = round((time.monotonic() - start) * 1000, 2)

        products = filter_products(raw_products)

        # Only the keys of params are logged, never its values: which fields a
        # caller searched on is the operational question, what they typed
        # into them is not.
        logger.info("Requested Audible Search", extra={
            "search_fields": sorted(params),
            "search_took": search_took,
            "region": region,
            "results": len(products),
        })

        if not products:
            return []

        # The search call already asked for full product metadata, so the
        # results are normalized directly and nothing is re-fetched.
        normalized = [normalize_product(p, region) for p in products]

    except NotFoundException:
        return []
    except Exception as e:
        logger.error("Search failed", extra={
            "region": region,
            "error_type": type(e).__name__,
        })
        # Not Audible answering that nothing matched: an empty list here would
        # be indistinguishable from a genuine zero-result search.
        raise as_audible_failure(e, "Audible search failed") from e

    if store is None:
        return settle_flags_list(normalized)
    # Written unsettled, as the writer needs the tri-state flags.
    written, _ = await _store.persist_books(store, normalized, region)
    return await _store.serve_merged(store, normalized, written, region)


async def _quick_search_books(
    get: AudibleGet, keywords: str, region: str, store: "LocalStore | None" = None
) -> list[dict[str, Any]]:
    """
    Resolves keywords through Audible's search suggestions and hydrates the
    ASINs they name; empty when nothing matched.

    When suggestions return nothing and the keywords look like a compound
    "Author - Series - Title" query, the first and last segments are searched
    as author and title. The hosted service also tries its stored books after
    that. Here, with a store, so does this: stored books matching that title
    and author answer a catalog leg that found nothing or could not run. With
    no store, or none stored, a transport failure in that leg is raised rather
    than swallowed, because an empty result would pass an outage off as a
    confirmed absence.
    """
    region = validate_region(region)
    if store is not None:
        await _store.check(store)
    try:
        start = time.monotonic()
        asins = await fetch_suggestion_asins(get, keywords, region)
        search_took = round((time.monotonic() - start) * 1000, 2)

        # The keywords are caller-authored text and are not logged; their
        # length is enough to correlate a slow or empty search with the size
        # of the query that produced it.
        logger.info("Requested Audible Quick Search", extra={
            "keywords_length": len(keywords),
            "search_took": search_took,
            "region": region,
            "suggestions_found": len(asins),
        })

        if asins:
            return (await hydrate_books(get, asins, region, store=store)).books

        if " - " in keywords:
            segments = [s.strip() for s in keywords.split(" - ") if s.strip()]
            if len(segments) >= 2:
                logger.info("Quick search compound fallback", extra={
                    "keywords_length": len(keywords),
                    "segments_found": len(segments),
                    "region": region,
                })
                title, author = segments[-1], segments[0]
                outage: AudibleAPIException | None = None
                try:
                    found = await _search_books(
                        get, region, title=title, author=author, limit=10, store=store
                    )
                except AudibleAPIException as exc:
                    if store is None:
                        raise
                    found, outage = [], exc
                if found or store is None:
                    return found
                stored = await _store.search_stored_books(store, title, author, region)
                if stored:
                    _store.log_served_from_store(
                        "book search", region,
                        stored_num=len(stored), catalog_failed=outage is not None,
                    )
                    return stored
                if outage is not None:
                    raise outage
                return []

        return []

    except NotFoundException:
        return []
    except Exception as e:
        logger.error("Quick search failed", extra={
            "region": region,
            "error_type": type(e).__name__,
        })
        raise as_audible_failure(e, "Audible quick search failed") from e


def _abs_region(region: str) -> str:
    """The Audiobookshelf-shaped routes answer an unknown region as an invalid
    request rather than as a region error."""
    try:
        return validate_region(region)
    except RegionException:
        raise NotFoundException("Invalid region", code=ErrorCode.INVALID_REQUEST) from None


def _require_matches(books: list[dict[str, Any]], message: str = _NOT_FOUND_MESSAGE) -> None:
    if not books:
        raise NotFoundException(message)


# ============================================================
# PUBLIC API
# ============================================================

async def search(
    get: AudibleGet,
    title: str | None = None,
    author: str | None = None,
    narrator: str | None = None,
    publisher: str | None = None,
    keywords: str | None = None,
    query: str | None = None,
    products_sort_by: str | None = None,
    limit: int = 10,
    page: int = 0,
    *,
    region: str = "us",
    store: "LocalStore | None" = None,
) -> list[BookResponse]:
    """
    Searches the Audible catalog.

    With a store the results are written through and served as the store holds
    them; a search Audible cannot answer is an outage, not a stored answer.

    query stands in for title when no title is given. Empty strings count as
    not given, and no filter at all is allowed; what Audible makes of that is
    Audible's answer. limit is 1..50 and page is 0..9, otherwise ValueError.
    Raises NotFoundException when nothing matched, AudibleAPIException when
    Audible could not be reached, and RegionException for an unknown region.
    """
    books = await _search_books(
        get,
        region,
        title=title or query,
        author=author,
        keywords=keywords,
        narrator=narrator,
        publisher=publisher,
        products_sort_by=products_sort_by,
        limit=limit,
        page=page,
        store=store,
    )
    _require_matches(books)
    return [BookResponse(**b) for b in books]


async def quick_search(
    get: AudibleGet,
    keywords: str,
    *,
    region: str = "us",
    store: "LocalStore | None" = None,
) -> list[BookResponse]:
    """
    Quick search through Audible's search suggestions, hydrated to full books.

    With a store the hydrated books are written through and served as the
    store holds them, a suggested book Audible could not give is answered from
    its stored copy, and a compound "Author - Title" query that the catalog
    could not answer is tried against the stored books.

    Raises NotFoundException when nothing matched, AudibleAPIException when
    Audible could not be reached, and RegionException for an unknown region.
    """
    books = await _quick_search_books(get, keywords, region, store)
    _require_matches(books)
    return [BookResponse(**b) for b in books]


async def abs_search(
    get: AudibleGet,
    title: str | None = None,
    query: str | None = None,
    author: str | None = None,
    keywords: str | None = None,
    *,
    region: str = "us",
    store: "LocalStore | None" = None,
) -> AbsSearchResponse:
    """
    The catalog search in the Audiobookshelf custom-metadata-provider shape:
    {"matches": [...]}, five results at most. query stands in for title.

    An unknown region is NotFoundException (code invalid_request) here, as on
    the hosted route, rather than RegionException. Otherwise as search.
    """
    region = _abs_region(region)
    books = await _search_books(
        get, region, title=title or query, author=author, keywords=keywords,
        limit=ABS_SEARCH_LIMIT, store=store,
    )
    _require_matches(books)
    return AbsSearchResponse(matches=[to_abs_book(b) for b in books])


async def abs_quick_search(
    get: AudibleGet,
    keywords: str | None = None,
    query: str | None = None,
    title: str | None = None,
    *,
    region: str = "us",
    store: "LocalStore | None" = None,
) -> AbsSearchResponse:
    """
    The quick search in the Audiobookshelf custom-metadata-provider shape.

    The first of keywords, query and title that is given is searched; none at
    all is NotFoundException (code invalid_request). An unknown region is the
    same, as on the hosted route. Otherwise as quick_search.
    """
    region = _abs_region(region)
    effective = keywords or query or title
    if not effective:
        raise NotFoundException("No search terms provided", code=ErrorCode.INVALID_REQUEST)
    books = await _quick_search_books(get, effective, region, store)
    _require_matches(books)
    return AbsSearchResponse(matches=[to_abs_book(b) for b in books])


async def narrator_books(
    get: AudibleGet,
    name: str,
    limit: int = 10,
    page: int = 0,
    *,
    region: str = "us",
    store: "LocalStore | None" = None,
) -> list[BookResponse]:
    """
    Books by narrator name, searched on Audible's narrator filter.

    Audible exposes narrators by name only. limit is 1..50 and page is 0..9,
    otherwise ValueError. Raises NotFoundException when nothing matched (the
    message does not repeat the name), AudibleAPIException when Audible could
    not be reached, and RegionException for an unknown region.
    """
    books = await _search_books(
        get, region, narrator=name, limit=limit, page=page, store=store
    )
    _require_matches(books, "No books found for narrator")
    return [BookResponse(**b) for b in books]
