"""
Database query endpoints.
Query the local database for indexed books without hitting Audible.
Only returns books that have been fetched and stored previously.
"""

# Standard library
from typing import Annotated, Any

# Third party
from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from app.api.routes.errors import ERROR_RESPONSES
from app.api.routes.large_response import build_large_list_response
from app.api.routes.narrators.schemas import NarratorProfileResponse
from app.core.middleware import valid_asin, valid_region
from app.db.session import get_session
from libex_core.audible.client import validate_region
from libex_core.exceptions import ErrorCode, NotFoundException
from libex_core.models import AuthorResponse, BookResponse, ChapterResponse, SeriesResponse
from app.api.routes.db.badge import badge_router
from app.api.routes.db.filters import (
    book_filters,
    NarratorFilters,
)
from app.api.routes.db.stats_headers import stats_cache_control
from app.api.routes.sort_params import (
    BookSortField,
    NarratorSortField,
    SortOrder,
)
from app.api.routes.release_params import ReleaseWindow
from app.services.db.reader import (
    get_author_books_from_db,
    get_author_from_db,
    get_book_from_db,
    get_books_by_plan_from_db,
    get_books_by_sku_from_db,
    get_db_stats,
    get_coming_soon_from_db,
    get_distinct_genres_from_db,
    get_distinct_plans_from_db,
    get_narrator_books_from_db,
    get_new_releases_from_db,
    get_series_books_from_db,
    get_series_from_db,
    get_track_from_db,
    get_vvab_books_from_db,
    search_narrators_from_db,
    search_books_from_db,
)

router = APIRouter(prefix="/db", tags=["Database"])

# The badge images that render /db/stats counts as SVG. Mounted here rather
# than registered separately in main.py so the db package keeps exporting a
# single router, and so the images cannot end up on a different prefix from
# the numbers they draw.
router.include_router(badge_router)

_RECORD_REGION_DESCRIPTION = (
    "Audible region code. A book or series is stored once per region, so the "
    "same ASIN can have a record in each. Omit to get the first-stored one; "
    "an invalid region is a 400, and a region the ASIN was never stored "
    "under is a 404 NOT_IN_LIBEX."
)


def optional_region(
    region: Annotated[str | None, Query(description=_RECORD_REGION_DESCRIPTION)] = None,
) -> str | None:
    """Validated region for a single-record read, or None when not given.

    Deliberately not valid_region: that one defaults to "us", and these reads
    must tell "no region asked for" (first-stored record) from "us".
    """
    return validate_region(region) if region is not None else None


class StatsResponse(BaseModel):
    """
    Counts of books, authors, narrators, series, and books with chapters.

    narrators has no region column and its PK is the name, so it is always a
    global count, even when `region` scopes the rest. Every series carries a
    region, so per-region series counts sum to the global series count.
    seriesRegionUnknown is kept for compatibility and is always 0 -- present
    when `region` scopes the response, null otherwise.

    books counts stored records, one per ASIN and region, so the per-region
    counts sum to it. distinctBookAsins counts the ASINs among them: smaller
    than books whenever a book is stored under more than one region, equal to
    it when `region` scopes the response.
    """

    books: int = 0
    distinctBookAsins: int = Field(default=0, ge=0)
    authors: int = 0
    narrators: int = 0
    series: int = 0
    booksWithChapters: int = 0
    region: str | None = None
    seriesRegionUnknown: int | None = None


@router.get("/stats", response_model=StatsResponse)
async def get_stats(
    response: Response,
    region: Annotated[
        str | None,
        Query(
            description=(
                "Audible region code. Omit for global counts. When given, "
                "scopes books/distinctBookAsins/authors/series/booksWithChapters to that "
                "region; narrators stays global (no region column), and "
                "series counts sum across regions to the global series "
                "count, and seriesRegionUnknown is always 0."
            )
        ),
    ] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """
    Get counts of books, authors, narrators, series, and books with chapters
    in the local DB.

    Public and unauthenticated, and no longer what a README render fetches:
    the counters are badge images now, 37 of them across 37 distinct origin
    URLs under /db/stats/badge/. This route is what each of those badges
    links to, so it is read when a reader clicks one, not when they open the
    page.

    The load moved but the cost did not, because the badges read the same
    get_db_stats cache entries as this route rather than ones of their own.
    A page render touches nine of those entries: the unscoped one behind all
    five global badges, and one `?region=xx` entry behind each of the eight
    regions' four counts. So what arrives together is nine entries' worth of
    first-request, not 37 -- and no contention across the nine, since each
    pays only what its own entry costs. What costs here is being the first
    request against a lapsed entry: a cold recompute over ~1.5M rows takes
    seconds where a warm read takes 0.12 at any concurrency. What it costs
    varies by entry, and region is not the largest term -- 1.6s for de, 4.7s
    for us, and 15.3s for the unscoped global count the five top-of-README
    badges read, that last one end-to-end wall clock and so an upper bound on
    database time rather than database time (stats_headers.py carries the
    full reading).

    Cache-Control has to be set explicitly: left unset, Cloudflare never
    caches this route -- `cf-cache-status: BYPASS` was measured on every call
    -- and every reader pays a recompute an edge copy would have covered. The
    header itself comes from stats_cache_control (stats_headers.py), shared
    with the badge route so the JSON and the image cannot advertise different
    freshness, and documented there.

    The one thing this route supplies is the expiry that policy is built
    from: the real remaining life of the cache entry get_db_stats already
    read or wrote, carried back on the result rather than re-read here (see
    DbStatsResult). None means nothing trustworthy was stored -- the
    DB-failure fallback, or a cache-write failure after an otherwise
    successful query -- and the response is marked no-store rather than
    handed the longest freshness Libex offers.
    """
    if region is not None:
        region = validate_region(region)
    result = await get_db_stats(session, region)

    response.headers["Cache-Control"] = stats_cache_control(result.cache_expires_at)
    return {**result.stats, "region": region}


@router.get("/book", response_model=list[BookResponse], responses=ERROR_RESPONSES)
async def search_db_books(
    filters=Depends(book_filters()),
    sort: Annotated[BookSortField | None, Query(description="Field to sort by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page (max 100)")] = 20,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    filter_kwargs = filters.as_kwargs()
    if not any(v is not None for v in filter_kwargs.values()) and sort is None:
        raise NotFoundException("No search parameters provided", code=ErrorCode.INVALID_REQUEST)

    books = await search_books_from_db(
        session=session,
        **filter_kwargs,
        sort=sort.value if sort is not None else None,
        order=order.value,
        limit=limit,
        page=page,
    )

    if not books:
        raise NotFoundException("No books found matching the given parameters", code=ErrorCode.NOT_IN_LIBEX)

    return books


@router.get("/plans", response_model=list[str], responses=ERROR_RESPONSES)
async def get_db_plans(
    session: AsyncSession = Depends(get_session),
) -> list[str]:
    """Get all distinct Audible plan names from the local DB."""
    plans = await get_distinct_plans_from_db(session)
    if not plans:
        raise NotFoundException("No plans found in local database", code=ErrorCode.NOT_IN_LIBEX)
    return plans


@router.get("/genres", response_model=list[str], responses=ERROR_RESPONSES)
async def get_db_genres(
    search: Annotated[str | None, Query(description="Filter genre names by partial match")] = None,
    session: AsyncSession = Depends(get_session),
) -> list[str]:
    """Get all distinct genre and tag names from the local DB.

    Use the optional search param to find specific categories before filtering
    other endpoints with the genre param.
    """
    genres = await get_distinct_genres_from_db(session, search=search)
    if not genres:
        raise NotFoundException("No genres found in local database", code=ErrorCode.NOT_IN_LIBEX)
    return genres


@router.get("/plans/{plan_name}", response_model=list[BookResponse], responses=ERROR_RESPONSES)
async def get_db_books_by_plan(
    plan_name: Annotated[str, Path(description="Audible plan name (e.g. US Minerva, AccessViaMusic)")],
    filters=Depends(book_filters(exclude={"plan_name"})),
    sort: Annotated[BookSortField | None, Query(description="Field to sort by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page (max 100)")] = 20,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Get all books available under a specific Audible plan from the local DB."""
    books = await get_books_by_plan_from_db(
        session,
        plan_name,
        **filters.as_kwargs(),
        sort=sort.value if sort is not None else None,
        order=order.value,
        limit=limit,
        page=page,
    )
    if not books:
        raise NotFoundException(f"No books found for plan: {plan_name}", code=ErrorCode.NOT_IN_LIBEX)
    return books


@router.get("/vvab", response_model=list[BookResponse], responses=ERROR_RESPONSES)
async def get_db_vvab_books(
    filters=Depends(book_filters(exclude={"is_vvab"})),
    sort: Annotated[BookSortField | None, Query(description="Field to sort by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page (max 100)")] = 20,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Get all virtual voice audiobooks (AI-narrated) from the local DB."""
    books = await get_vvab_books_from_db(
        session,
        **filters.as_kwargs(),
        sort=sort.value if sort is not None else None,
        order=order.value,
        limit=limit,
        page=page,
    )
    if not books:
        raise NotFoundException("No virtual voice audiobooks found in local database", code=ErrorCode.NOT_IN_LIBEX)
    return books


@router.get("/new-releases", response_model=list[BookResponse], responses=ERROR_RESPONSES)
async def get_db_new_releases(
    days: Annotated[ReleaseWindow, Query(description="Look-back window in days")] = ReleaseWindow.days_30,
    filters=Depends(book_filters()),
    sort: Annotated[BookSortField | None, Query(description="Field to sort by (defaults to newest first)")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.desc,
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page (max 100)")] = 20,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """
    Get books released within the look-back window, newest first.

    Already-released books only — far-future pre-orders are excluded. Defaults
    to releaseDate descending; pass a sort field to override.
    """
    books = await get_new_releases_from_db(
        session,
        days=days.value,
        **filters.as_kwargs(),
        sort=sort.value if sort is not None else None,
        order=order.value,
        limit=limit,
        page=page,
    )
    if not books:
        raise NotFoundException("No new releases found in local database", code=ErrorCode.NOT_IN_LIBEX)
    return books


@router.get("/coming-soon", response_model=list[BookResponse], responses=ERROR_RESPONSES)
async def get_db_coming_soon(
    days: Annotated[ReleaseWindow, Query(description="Look-ahead window in days")] = ReleaseWindow.days_30,
    filters=Depends(book_filters()),
    sort: Annotated[BookSortField | None, Query(description="Field to sort by (defaults to soonest first)")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page (max 100)")] = 20,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """
    Get upcoming books releasing within the look-ahead window, soonest first.

    Future releases only. The window also excludes Audible's "no date yet"
    placeholder, so only books with a real upcoming date show up. Defaults to
    releaseDate ascending; pass a sort field to override.
    """
    books = await get_coming_soon_from_db(
        session,
        days=days.value,
        **filters.as_kwargs(),
        sort=sort.value if sort is not None else None,
        order=order.value,
        limit=limit,
        page=page,
    )
    if not books:
        raise NotFoundException("No upcoming releases found in local database", code=ErrorCode.NOT_IN_LIBEX)
    return books


@router.get("/book/sku/{sku}", response_model=list[BookResponse], responses=ERROR_RESPONSES)
async def get_db_books_by_sku(
    sku: Annotated[str, Path(description="SKU group identifier")],
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Get all region variants for a SKU group from the local DB."""
    books = await get_books_by_sku_from_db(session, sku)
    if not books:
        raise NotFoundException("No books found for SKU", code=ErrorCode.NOT_IN_LIBEX)
    return books


@router.get("/book/{asin}/chapters", response_model=ChapterResponse, responses=ERROR_RESPONSES)
async def get_db_book_chapters(
    asin: Annotated[str, Depends(valid_asin("Book ASIN"))],
    region: str | None = Depends(optional_region),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Get chapter data for a book from the local DB.

    Without `region`, the first-stored region's chapters; with it, that
    region's, or 404 when none are stored for it.
    """
    chapters = await get_track_from_db(session, asin, region=region)
    if chapters is None:
        raise NotFoundException("No chapter data found for this book", code=ErrorCode.NOT_IN_LIBEX)
    return chapters


@router.get("/book/{asin}", response_model=BookResponse, responses=ERROR_RESPONSES)
async def get_db_book(
    asin: Annotated[str, Depends(valid_asin("Book ASIN"))],
    region: str | None = Depends(optional_region),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Get a single book by ASIN from the local DB.

    Without `region`, the first-stored region's record; with it, that
    region's, or 404 when the book was never stored under it.
    """
    book = await get_book_from_db(session, asin, region=region)
    if not book:
        raise NotFoundException("Book not found in local database", code=ErrorCode.NOT_IN_LIBEX)
    return book


@router.get("/author/{asin}/books", response_model=list[BookResponse], responses=ERROR_RESPONSES)
async def get_db_author_books(
    asin: Annotated[str, Depends(valid_asin("Author ASIN"))],
    region: str = Depends(valid_region),
    filters=Depends(book_filters(exclude={"region", "author_name"})),
    book_region: Annotated[str | None, Query(description="Optional filter; without it every linked book is returned")] = None,
    sort: Annotated[BookSortField | None, Query(description="Field to sort by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse] | Response:
    """Get all books by an author from the local DB."""
    books = await get_author_books_from_db(
        session,
        asin,
        region,
        book_region=book_region,
        **filters.as_kwargs(),
        sort=sort.value if sort is not None else None,
        order=order.value,
    )
    if not books:
        raise NotFoundException("No books found for author", code=ErrorCode.NOT_IN_LIBEX)
    return await build_large_list_response(
        list[BookResponse], len(books), lambda: [BookResponse(**b) for b in books]
    )


@router.get("/author/{asin}", response_model=AuthorResponse, responses=ERROR_RESPONSES)
async def get_db_author(
    asin: Annotated[str, Depends(valid_asin("Author ASIN"))],
    region: str = Depends(valid_region),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Get an author by ASIN from the local DB."""
    author = await get_author_from_db(session, asin, region)
    if not author:
        raise NotFoundException("Author not found in local database", code=ErrorCode.NOT_IN_LIBEX)
    return author


@router.get("/narrator/books", response_model=list[BookResponse], responses=ERROR_RESPONSES)
async def get_db_narrator_books(
    name: Annotated[str, Query(description="Narrator name (exact match)")],
    filters=Depends(book_filters()),
    sort: Annotated[BookSortField | None, Query(description="Field to sort by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page (max 100)")] = 20,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Get all books by a narrator from the local DB."""
    books = await get_narrator_books_from_db(
        session,
        name,
        **filters.as_kwargs(),
        sort=sort.value if sort is not None else None,
        order=order.value,
        limit=limit,
        page=page,
    )
    if not books:
        raise NotFoundException(f"No books found for narrator: {name}", code=ErrorCode.NOT_IN_LIBEX)
    return books


@router.get("/narrator", response_model=list[NarratorProfileResponse], responses=ERROR_RESPONSES)
async def search_db_narrators(
    name: Annotated[str, Query(description="Narrator name to search for")],
    filters: NarratorFilters = Depends(),
    sort: Annotated[NarratorSortField | None, Query(description="Field to sort by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page (max 100)")] = 20,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Search narrators by name from the local DB."""
    narrators = await search_narrators_from_db(
        session,
        name,
        **filters.as_kwargs(),
        sort=sort.value if sort is not None else None,
        order=order.value,
        limit=limit,
        page=page,
    )
    if not narrators:
        raise NotFoundException(f"No narrators found matching: {name}", code=ErrorCode.NOT_IN_LIBEX)
    return narrators


@router.get("/series/{asin}/books", response_model=list[BookResponse], responses=ERROR_RESPONSES)
async def get_db_series_books(
    asin: Annotated[str, Depends(valid_asin("Series ASIN"))],
    filters=Depends(book_filters(exclude={"series_name"})),
    sort: Annotated[BookSortField | None, Query(description="Field to sort by (overrides default position order)")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse] | Response:
    """Get all books in a series from the local DB.

    Defaults to series position order; passing a sort field overrides it.
    """
    books = await get_series_books_from_db(
        session,
        asin,
        **filters.as_kwargs(),
        sort=sort.value if sort is not None else None,
        order=order.value,
    )
    if not books:
        raise NotFoundException("No books found for series", code=ErrorCode.NOT_IN_LIBEX)
    return await build_large_list_response(
        list[BookResponse], len(books), lambda: [BookResponse(**b) for b in books]
    )


@router.get("/series/{asin}", response_model=SeriesResponse, responses=ERROR_RESPONSES)
async def get_db_series(
    asin: Annotated[str, Depends(valid_asin("Series ASIN"))],
    region: str | None = Depends(optional_region),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Get a series by ASIN from the local DB.

    Without `region`, the first-stored region's record; with it, that
    region's, or 404 when the series was never stored under it.
    """
    series = await get_series_from_db(session, asin, region=region)
    if not series:
        raise NotFoundException("Series not found in local database", code=ErrorCode.NOT_IN_LIBEX)
    return series
