"""
Database query endpoints.
Query the local database for indexed books without hitting Audible.
Only returns books that have been fetched and stored previously.
"""

# Standard library
from datetime import datetime, timezone
from typing import Annotated, Any

# Third party
from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from app.api.routes.authors.schemas import AuthorResponse
from app.api.routes.books.schemas import BookResponse, ChapterResponse
from app.api.routes.large_response import build_large_list_response
from app.api.routes.narrators.schemas import NarratorProfileResponse
from app.api.routes.series.schemas import SeriesResponse
from app.core.exceptions import NotFoundException
from app.core.middleware import is_valid_asin, valid_region
from app.db.session import get_session
from app.services.audible.client import validate_region
from app.api.routes.db.filters import (
    book_filters,
    NarratorFilters,
)
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


class StatsResponse(BaseModel):
    """
    Counts of books, authors, narrators, series, and books with chapters.

    narrators has no region column and its PK is the name, so it is always a
    global count, even when `region` scopes the rest. series.region is
    nullable; a scoped series count excludes rows with no region, so
    per-region series counts will not sum to the global series count.
    seriesRegionUnknown is that excluded count -- present when `region` scopes
    the response, null otherwise.
    """

    books: int = 0
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
                "scopes books/authors/series/booksWithChapters to that "
                "region; narrators stays global (no region column), and "
                "series excludes rows with no region so it will not sum to "
                "the global series count."
            )
        ),
    ] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """
    Get counts of books, authors, narrators, series, and books with chapters
    in the local DB.

    Public, unauthenticated, and hit hard by shields.io on every README
    render -- 37 fetches across 9 distinct origin URLs (5 global badges plus
    8 regions, each region's 4 counts sharing one `?region=xx` response via
    different JSONPaths) fired at once by one page load. Cache-Control has
    to be set explicitly here: left unset, Cloudflare never caches this
    route -- `cf-cache-status: BYPASS` was measured on every call -- and
    those badges render "inaccessible" instead of a number.

    s-maxage and max-age carry the same value. There is a blast-radius gap
    between an edge copy and a browser copy -- the same one author-books
    cites, where an edge copy is purgeable and a browser copy is not --
    but it is safe to ignore here because the value handed to both is
    bounded above by STATS_CACHE_TTL_SECONDS: neither copy can ever be told
    to hold stale data longer than that ceiling permits.

    The value itself is the real remaining life of the cache entry
    get_db_stats already read or wrote, carried back on the result rather
    than re-read independently here (see DbStatsResult): quoting the full
    TTL regardless of how far into its life the underlying entry already is
    would tell the edge to hold a copy for a fresh window measured from
    whenever it happened to ask, which can leave the edge serving a copy
    well after origin has already moved on to a newer one. When nothing
    trustworthy was stored -- the DB-failure fallback, or a cache-write
    failure after an otherwise successful query -- get_db_stats reports
    that as no expiry at all, and the response is marked no-store rather
    than handed the longest freshness Libex offers.
    """
    if region is not None:
        region = validate_region(region)
    result = await get_db_stats(session, region)

    if result.cache_expires_at is None:
        response.headers["Cache-Control"] = "no-store"
    else:
        remaining = (result.cache_expires_at - datetime.now(timezone.utc)).total_seconds()
        edge_seconds = max(0, int(remaining))
        response.headers["Cache-Control"] = f"public, max-age={edge_seconds}, s-maxage={edge_seconds}"

    return {**result.stats, "region": region}


@router.get("/book", response_model=list[BookResponse])
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
        raise NotFoundException("No search parameters provided")

    books = await search_books_from_db(
        session=session,
        **filter_kwargs,
        sort=sort.value if sort is not None else None,
        order=order.value,
        limit=limit,
        page=page,
    )

    if not books:
        raise NotFoundException("No books found matching the given parameters")

    return books


@router.get("/plans", response_model=list[str])
async def get_db_plans(
    session: AsyncSession = Depends(get_session),
) -> list[str]:
    """Get all distinct Audible plan names from the local DB."""
    plans = await get_distinct_plans_from_db(session)
    if not plans:
        raise NotFoundException("No plans found in local database")
    return plans


@router.get("/genres", response_model=list[str])
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
        raise NotFoundException("No genres found in local database")
    return genres


@router.get("/plans/{plan_name}", response_model=list[BookResponse])
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
        raise NotFoundException(f"No books found for plan: {plan_name}")
    return books


@router.get("/vvab", response_model=list[BookResponse])
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
        raise NotFoundException("No virtual voice audiobooks found in local database")
    return books


@router.get("/new-releases", response_model=list[BookResponse])
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
        raise NotFoundException("No new releases found in local database")
    return books


@router.get("/coming-soon", response_model=list[BookResponse])
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
        raise NotFoundException("No upcoming releases found in local database")
    return books


@router.get("/book/sku/{sku}", response_model=list[BookResponse])
async def get_db_books_by_sku(
    sku: Annotated[str, Path(description="SKU group identifier")],
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Get all region variants for a SKU group from the local DB."""
    books = await get_books_by_sku_from_db(session, sku)
    if not books:
        raise NotFoundException("No books found for SKU")
    return books


@router.get("/book/{asin}/chapters", response_model=ChapterResponse)
async def get_db_book_chapters(
    asin: Annotated[str, Path(description="Book ASIN")],
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Get chapter data for a book from the local DB."""
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    chapters = await get_track_from_db(session, asin)
    if chapters is None:
        raise NotFoundException("No chapter data found for this book")
    return chapters


@router.get("/book/{asin}", response_model=BookResponse)
async def get_db_book(
    asin: Annotated[str, Path(description="Book ASIN")],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Get a single book by ASIN from the local DB."""
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    book = await get_book_from_db(session, asin)
    if not book:
        raise NotFoundException("Book not found in local database")
    return book


@router.get("/author/{asin}/books", response_model=list[BookResponse])
async def get_db_author_books(
    asin: Annotated[str, Path(description="Author ASIN")],
    region: str = Depends(valid_region),
    filters=Depends(book_filters(exclude={"region", "author_name"})),
    book_region: Annotated[str | None, Query(description="Filter the author's books by their region")] = None,
    sort: Annotated[BookSortField | None, Query(description="Field to sort by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse] | Response:
    """Get all books by an author from the local DB."""
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
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
        raise NotFoundException("No books found for author")
    return await build_large_list_response(
        list[BookResponse], len(books), lambda: [BookResponse(**b) for b in books]
    )


@router.get("/author/{asin}", response_model=AuthorResponse)
async def get_db_author(
    asin: Annotated[str, Path(description="Author ASIN")],
    region: str = Depends(valid_region),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Get an author by ASIN from the local DB."""
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    author = await get_author_from_db(session, asin, region)
    if not author:
        raise NotFoundException("Author not found in local database")
    return author


@router.get("/narrator/books", response_model=list[BookResponse])
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
        raise NotFoundException(f"No books found for narrator: {name}")
    return books


@router.get("/narrator", response_model=list[NarratorProfileResponse])
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
        raise NotFoundException(f"No narrators found matching: {name}")
    return narrators


@router.get("/series/{asin}/books", response_model=list[BookResponse])
async def get_db_series_books(
    asin: Annotated[str, Path(description="Series ASIN")],
    filters=Depends(book_filters(exclude={"series_name"})),
    sort: Annotated[BookSortField | None, Query(description="Field to sort by (overrides default position order)")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse] | Response:
    """Get all books in a series from the local DB.

    Defaults to series position order; passing a sort field overrides it.
    """
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    books = await get_series_books_from_db(
        session,
        asin,
        **filters.as_kwargs(),
        sort=sort.value if sort is not None else None,
        order=order.value,
    )
    if not books:
        raise NotFoundException("No books found for series")
    return await build_large_list_response(
        list[BookResponse], len(books), lambda: [BookResponse(**b) for b in books]
    )


@router.get("/series/{asin}", response_model=SeriesResponse)
async def get_db_series(
    asin: Annotated[str, Path(description="Series ASIN")],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Get a series by ASIN from the local DB."""
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    series = await get_series_from_db(session, asin)
    if not series:
        raise NotFoundException("Series not found in local database")
    return series