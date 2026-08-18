"""
Authors router.
Compatible with AudiMeta endpoint structure for drop-in replacement.
"""

# Standard library
from datetime import datetime, timezone
from typing import Annotated

# Third party
from fastapi import APIRouter, Query, Path, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.session import get_session

# Routes
from app.api.routes.authors.schemas import AuthorResponse
from app.api.routes.books.schemas import BookResponse
from app.api.routes.sort_params import BookSortField, SortOrder
from app.api.routes.filter_params import LiveBookFilters

# Services
from app.services.audible.authors import (
    get_author,
    get_author_books,
    get_author_books_by_name,
    search_authors,
)
from app.services.audible.books import get_books_by_asins
from app.services.sorting import sort_dicts, BOOK_SORT_FIELDS
from app.services.filtering import filter_dicts

# Core
from app.core.middleware import is_valid_asin, valid_region
from app.core.exceptions import NotFoundException

router = APIRouter(prefix="/author", tags=["Authors"])

# ============================================================
# ENDPOINTS
# ============================================================

@router.get("", response_model=list[AuthorResponse])
async def search(
    name: Annotated[str, Query(description="Author name to search for")],
    region: str = Depends(valid_region),
    session: AsyncSession = Depends(get_session),
) -> list[AuthorResponse]:
    """Search for authors by name. Returns 404 if none found."""
    authors = await search_authors(name, region, session)
    if not authors:
        raise NotFoundException("No authors found")
    return [AuthorResponse(**a) for a in authors]


@router.get("/books", response_model=list[BookResponse])
async def get_books_by_author_name(
    name: Annotated[str, Query(description="Author name")],
    region: str = Depends(valid_region),
    filters: LiveBookFilters = Depends(),
    sort: Annotated[BookSortField | None, Query(description="Field to sort the returned books by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """
    Get books by author name.
    Used when no author ASIN is available.
    Returns full book objects matching AudiMeta's BookDto format.
    """
    asins = await get_author_books_by_name(name, region, session)
    if not asins:
        raise NotFoundException("No books found for author")
    books = await get_books_by_asins(asins, region, session)
    books = filter_dicts(books, filters.as_kwargs())
    books = sort_dicts(books, sort.value if sort is not None else None, order.value, BOOK_SORT_FIELDS)
    return [BookResponse(**b) for b in books]


# Browser cache for a complete author-books response. Short on purpose: a
# browser holding a stale catalogue has no way to be purged, where the edge
# copy expires on a schedule Libex sets below and can be purged at
# Cloudflare. s-maxage overrides this for shared caches, so this figure only
# ever governs the private copy.
_BROWSER_CACHE_SECONDS = 300

# Edge TTL for a complete result that was walked just now rather than read
# from cache.
#
# Short, and the reason is not caution for its own sake. A fresh walk's
# cache entry is written by a BACKGROUND task that has not necessarily
# committed by the time this response goes out, and can fail outright. There
# is therefore no remaining-life figure to advertise that is known to
# correspond to anything stored. Advertising the full day here would let the
# edge hold, and Tiered Cache spread, a copy Libex may never have persisted.
# Five minutes bounds that to something self-correcting: the next request
# after it lapses reads Libex's own entry and gets the exact aligned expiry
# below.
_FRESH_WALK_EDGE_CACHE_SECONDS = 300


def _mark_completeness(
    response: Response,
    is_complete: bool,
    cache_expires_at: datetime | None,
    use_cache: bool,
) -> None:
    """
    Tells the caller, and any cache in front of Libex, what this response is
    and how long it may be held.

    Completeness travels in a header rather than the body, because the
    response is a bare list[BookResponse] and that shape is a drop-in
    compatibility contract -- adding a field would change it for every
    consumer. A header is purely additive: a client that ignores it behaves
    exactly as before.

    The status stays 200. 206 was considered and rejected: HTTP already
    assigns it to range requests and requires Content-Range with it, so
    using it to mean "this data is incomplete" is both non-conformant and an
    invitation for a CDN to route the response down its partial-content
    path -- the last thing wanted on the endpoint whose edge caching this
    exists to make safe.

    An incomplete response is refused to caches outright. Libex's fronting
    Cloudflare rule is configured to use the origin's cache-control when one
    is present and to bypass when none is, so no-store here is honoured
    rather than advisory -- measured 2026-08-17, every API path returned
    cf-cache-status: BYPASS precisely because Libex sent no cache-control at
    all. With Tiered Cache enabled a cached object is shared across upper
    tiers as well, so a partial that slipped through would be wrong at
    considerably more than one PoP.

    A complete response served FROM the cache advertises exactly the life
    remaining on Libex's own entry, so the two expire together instead of
    the edge drifting past on a fixed timer. One served from a walk taken
    just now gets _FRESH_WALK_EDGE_CACHE_SECONDS instead -- see that
    constant for why a just-walked result has no trustworthy remaining life
    to quote.
    """
    response.headers["X-Libex-Complete"] = "true" if is_complete else "false"

    if not is_complete:
        response.headers["Cache-Control"] = "no-store"
        return

    # cache=false has to reach this layer too, or the escape hatch only holds
    # as far as Libex's own storage. A caller who asks for an uncached answer
    # and is handed one marked publicly cacheable gets that same answer back
    # from their browser or the CDN on the next identical request -- the edge
    # keys on the query string, so ?cache=false becomes its own cached object
    # and the request never reaches here again for the whole TTL. The flag
    # already governs discovery and hydration; stopping one layer short is
    # exactly the half-wired shape this endpoint has been bitten by before.
    if not use_cache:
        response.headers["Cache-Control"] = "no-store"
        return

    if cache_expires_at is None:
        edge_seconds = _FRESH_WALK_EDGE_CACHE_SECONDS
    else:
        remaining = (cache_expires_at - datetime.now(timezone.utc)).total_seconds()
        # An entry read as live can still be a hair from expiry by the time
        # this arithmetic runs, and a negative or zero s-maxage would be
        # nonsense to advertise. Floor rather than fall back to the fresh
        # figure: the entry really is about to lapse, and saying so is the
        # honest answer.
        edge_seconds = max(0, int(remaining))

    response.headers["Cache-Control"] = (
        f"public, max-age={_BROWSER_CACHE_SECONDS}, s-maxage={edge_seconds}"
    )


@router.get("/books/{asin}", response_model=list[BookResponse])
async def get_books_by_author(
    asin: Annotated[str, Path(description="Author ASIN")],
    response: Response,
    region: str = Depends(valid_region),
    # Defaults to True, which is the point of the flag rather than an
    # incidental choice. Defaulted False, the cache only ever served callers
    # who explicitly asked for it, so there was no such thing as a warm
    # request on the default public path: every request walked Audible in
    # full, and a prolific author's walk runs to hundreds of upstream
    # requests and 504s behind the proxy's 30s timeout. The walk already
    # WRITES its result unconditionally -- see _walk_author_books' call to
    # persist_author_books_cache_background -- so only the read was gated,
    # and the cache was being populated for almost nobody.
    #
    # Single-flight is not a substitute and must not be read as one: it
    # collapses CONCURRENT duplicates only. Ten callers in one instant were
    # already a single walk; ten callers a minute apart were ten walks.
    #
    # The DB is not a substitute either. It is already unioned into every
    # walk as the fourth source, so an author being stored does not spare
    # anyone the walk -- only a cache hit does.
    #
    # cache=false keeps its documented meaning and still forces the full
    # walk, so a caller who genuinely needs an uncached answer has one. That
    # leaves the expensive path reachable by anyone, Libex having neither
    # auth nor rate limiting by design -- but it is reachable today as the
    # default for everyone, so this narrows the exposure rather than opening
    # anything.
    cache: Annotated[bool, Query(description="Return cached data if available")] = True,
    filters: LiveBookFilters = Depends(),
    sort: Annotated[BookSortField | None, Query(description="Field to sort the returned books by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """
    Get all books by author ASIN.
    Returns full book objects matching AudiMeta's BookDto format.
    """
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    walk = await get_author_books(asin, region, session, cache)
    asins = walk.asins
    if not asins:
        raise NotFoundException("No books found for author")
    # use_cache=cache: hydration is the second half of the same request
    # discovery just served from cache/DB above -- carrying the same flag
    # through keeps that a single decision instead of two, matching the
    # load-shedding intent (first hit reaches Audible, everything for the
    # next 24h is served from DB/cache).
    #
    # high_concurrency=True: this hydration is the second half of the same
    # live author-books request get_author_books' own discovery walk just
    # ran, and a live, measured production outage traced directly to that
    # pairing running serialized behind the default Audible concurrency pool
    # -- see get_books_by_asins' own docstring and client.py's
    # AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT for the measurements.
    books = await get_books_by_asins(asins, region, session, use_cache=cache, high_concurrency=True)
    # Marked here rather than above, and given the hydrated books rather than
    # the walk alone. walk.is_complete describes DISCOVERY -- whether the ASIN
    # list is whole -- while the body a caller receives is what hydration
    # returned, and get_books_by_asins has three documented paths that return
    # fewer books than it was handed: chunks that failed transiently and were
    # only partly recovered from the DB backstop, the outage fallback, and
    # ASINs Audible no longer knows. Marking on discovery alone advertised a
    # half-hydrated body as complete and, once these responses became
    # cacheable, let an edge hold that body for the entry's whole remaining
    # life. Counted before filtering, since filters legitimately shrink the
    # list and say nothing about whether the fetch succeeded.
    _mark_completeness(
        response,
        walk.is_complete and len(books) == len(asins),
        walk.cache_expires_at,
        cache,
    )
    books = filter_dicts(books, filters.as_kwargs())
    books = sort_dicts(books, sort.value if sort is not None else None, order.value, BOOK_SORT_FIELDS)
    return [BookResponse(**b) for b in books]


@router.get("/{asin}/books", response_model=list[BookResponse], include_in_schema=False)
async def get_books_by_author_primary(
    asin: Annotated[str, Path(description="Author ASIN")],
    response: Response,
    region: str = Depends(valid_region),
    # Defaults to True for the reasons given on get_books_by_author above.
    # This is its legacy-route twin and the two must never disagree on it.
    cache: Annotated[bool, Query(description="Return cached data if available")] = True,
    filters: LiveBookFilters = Depends(),
    sort: Annotated[BookSortField | None, Query(description="Field to sort the returned books by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """Legacy endpoint. Use /author/books/{asin} instead."""
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    walk = await get_author_books(asin, region, session, cache)
    asins = walk.asins
    if not asins:
        raise NotFoundException("No books found for author")
    # use_cache=cache and high_concurrency=True: same pairing as
    # get_books_by_author above (this is its legacy-route twin) -- see that
    # call site's comments.
    books = await get_books_by_asins(asins, region, session, use_cache=cache, high_concurrency=True)
    # Marked here rather than above, and given the hydrated books rather than
    # the walk alone. walk.is_complete describes DISCOVERY -- whether the ASIN
    # list is whole -- while the body a caller receives is what hydration
    # returned, and get_books_by_asins has three documented paths that return
    # fewer books than it was handed: chunks that failed transiently and were
    # only partly recovered from the DB backstop, the outage fallback, and
    # ASINs Audible no longer knows. Marking on discovery alone advertised a
    # half-hydrated body as complete and, once these responses became
    # cacheable, let an edge hold that body for the entry's whole remaining
    # life. Counted before filtering, since filters legitimately shrink the
    # list and say nothing about whether the fetch succeeded.
    _mark_completeness(
        response,
        walk.is_complete and len(books) == len(asins),
        walk.cache_expires_at,
        cache,
    )
    books = filter_dicts(books, filters.as_kwargs())
    books = sort_dicts(books, sort.value if sort is not None else None, order.value, BOOK_SORT_FIELDS)
    return [BookResponse(**b) for b in books]


@router.get("/{asin}", response_model=AuthorResponse)
async def get_author_by_asin(
    asin: Annotated[str, Path(description="Author ASIN")],
    region: str = Depends(valid_region),
    cache: Annotated[bool, Query(description="Return cached data if available")] = False,
    session: AsyncSession = Depends(get_session),
) -> AuthorResponse:
    """Get author profile by ASIN."""
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    data = await get_author(asin, region, session, cache)
    return AuthorResponse(**data)