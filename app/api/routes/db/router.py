"""
Database query endpoints.
Query the local database for indexed books without hitting Audible.
Only returns books that have been fetched and stored previously.
"""

# Standard library
from enum import IntEnum
from typing import Annotated, Any

# Third party
from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from app.api.routes.authors.schemas import AuthorResponse
from app.api.routes.books.schemas import BookResponse, ChapterResponse
from app.api.routes.narrators.schemas import NarratorProfileResponse
from app.api.routes.series.schemas import SeriesResponse
from app.core.exceptions import NotFoundException
from app.core.middleware import is_valid_asin, valid_region
from app.db.session import get_session
from app.api.routes.db.filters import (
    book_filters,
    NarratorFilters,
)
from app.api.routes.sort_params import (
    BookSortField,
    NarratorSortField,
    SortOrder,
)
from app.services.db.reader import (
    get_author_books_from_db,
    get_author_from_db,
    get_book_from_db,
    get_books_by_plan_from_db,
    get_books_by_sku_from_db,
    get_db_stats,
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


class NewReleaseWindow(IntEnum):
    """Allowed look-back windows (in days) for the new-releases endpoint."""
    days_30 = 30
    days_60 = 60
    days_90 = 90
    days_120 = 120
    days_240 = 240
    days_365 = 365


class StatsResponse(BaseModel):
    books: int = 0
    authors: int = 0
    narrators: int = 0
    series: int = 0


@router.get("/stats", response_model=StatsResponse)
async def get_stats(
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Get counts of books, authors, narrators, and series in the local DB."""
    return await get_db_stats(session)


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
    days: Annotated[NewReleaseWindow, Query(description="Look-back window in days")] = NewReleaseWindow.days_30,
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
) -> list[dict[str, Any]]:
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
    return books


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
) -> list[dict[str, Any]]:
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
    return books


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