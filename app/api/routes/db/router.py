"""
Database query endpoints.
Query the local database for indexed books without hitting Audible.
Only returns books that have been fetched and stored previously.
"""

# Standard library
from typing import Annotated, Any

# Third party
from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from app.api.routes.authors.schemas import AuthorResponse
from app.api.routes.books.schemas import BookResponse
from app.api.routes.series.schemas import SeriesResponse
from app.core.exceptions import NotFoundException
from app.core.middleware import is_valid_asin, valid_region
from app.db.session import get_session
from app.services.db.reader import (
    get_author_books_from_db,
    get_author_from_db,
    get_book_from_db,
    get_books_by_plan_from_db,
    get_books_by_sku_from_db,
    get_db_stats,
    get_distinct_plans_from_db,
    get_narrator_books_from_db,
    get_series_books_from_db,
    get_series_from_db,
    get_track_from_db,
    get_vvab_books_from_db,
    search_narrators_from_db,
    search_books_from_db,
)

router = APIRouter(prefix="/db", tags=["Database"])


@router.get("/stats")
async def get_stats(
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Get counts of books, authors, narrators, and series in the local DB."""
    return await get_db_stats(session)


@router.get("/book", response_model=list[BookResponse])
async def search_db_books(
    title: Annotated[str | None, Query(description="Filter by title")] = None,
    subtitle: Annotated[str | None, Query(description="Filter by subtitle")] = None,
    region: Annotated[str | None, Query(description="Filter by region")] = None,
    description: Annotated[str | None, Query(description="Filter by description")] = None,
    summary: Annotated[str | None, Query(description="Filter by summary")] = None,
    publisher: Annotated[str | None, Query(description="Filter by publisher")] = None,
    copyright: Annotated[str | None, Query(description="Filter by copyright")] = None,
    isbn: Annotated[str | None, Query(description="Filter by ISBN")] = None,
    author_name: Annotated[str | None, Query(description="Filter by author name")] = None,
    series_name: Annotated[str | None, Query(description="Filter by series name")] = None,
    language: Annotated[str | None, Query(description="Filter by language")] = None,
    rating_better_than: Annotated[float | None, Query(description="Minimum rating")] = None,
    rating_worse_than: Annotated[float | None, Query(description="Maximum rating")] = None,
    longer_than: Annotated[int | None, Query(description="Minimum length in minutes")] = None,
    shorter_than: Annotated[int | None, Query(description="Maximum length in minutes")] = None,
    explicit: Annotated[bool | None, Query(description="Filter by explicit")] = None,
    whisper_sync: Annotated[bool | None, Query(description="Filter by Whispersync availability")] = None,
    has_pdf: Annotated[bool | None, Query(description="Filter by PDF companion availability")] = None,
    book_format: Annotated[str | None, Query(description="Filter by book format")] = None,
    content_type: Annotated[str | None, Query(description="Filter by content type")] = None,
    content_delivery_type: Annotated[str | None, Query(description="Filter by content delivery type")] = None,
    is_listenable: Annotated[bool | None, Query(description="Filter by listenable status")] = None,
    is_buyable: Annotated[bool | None, Query(description="Filter by buyable status")] = None,
    is_vvab: Annotated[bool | None, Query(description="Filter by VVAB (virtual voice audiobook) status")] = None,
    plan_name: Annotated[str | None, Query(description="Filter by Audible plan name (e.g. US Minerva, AccessViaMusic)")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page (max 100)")] = 20,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    filter_params = [
        title, subtitle, region, description, summary, publisher, copyright,
        isbn, author_name, series_name, language, rating_better_than, rating_worse_than, longer_than,
        shorter_than, explicit, whisper_sync, has_pdf, book_format,
        content_type, content_delivery_type, is_listenable, is_buyable, is_vvab, plan_name,
    ]
    if not any(p is not None for p in filter_params):
        raise NotFoundException("No search parameters provided")

    books = await search_books_from_db(
        session=session,
        title=title,
        subtitle=subtitle,
        region=region,
        description=description,
        summary=summary,
        publisher=publisher,
        copyright=copyright,
        isbn=isbn,
        author_name=author_name,
        series_name=series_name,
        language=language,
        rating_better_than=rating_better_than,
        rating_worse_than=rating_worse_than,
        longer_than=longer_than,
        shorter_than=shorter_than,
        explicit=explicit,
        whisper_sync=whisper_sync,
        has_pdf=has_pdf,
        book_format=book_format,
        content_type=content_type,
        content_delivery_type=content_delivery_type,
        is_listenable=is_listenable,
        is_buyable=is_buyable,
        is_vvab=is_vvab,
        plan_name=plan_name,
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


@router.get("/plans/{plan_name}", response_model=list[BookResponse])
async def get_db_books_by_plan(
    plan_name: Annotated[str, Path(description="Audible plan name (e.g. US Minerva, AccessViaMusic)")],
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page (max 100)")] = 20,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Get all books available under a specific Audible plan from the local DB."""
    books = await get_books_by_plan_from_db(session, plan_name, limit=limit, page=page)
    if not books:
        raise NotFoundException(f"No books found for plan: {plan_name}")
    return books


@router.get("/vvab", response_model=list[BookResponse])
async def get_db_vvab_books(
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page (max 100)")] = 20,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Get all virtual voice audiobooks (AI-narrated) from the local DB."""
    books = await get_vvab_books_from_db(session, limit=limit, page=page)
    if not books:
        raise NotFoundException("No virtual voice audiobooks found in local database")
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


@router.get("/book/{asin}/chapters")
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
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Get all books by an author from the local DB."""
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    books = await get_author_books_from_db(session, asin, region)
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
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page (max 100)")] = 20,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Get all books by a narrator from the local DB."""
    books = await get_narrator_books_from_db(session, name, limit=limit, page=page)
    if not books:
        raise NotFoundException(f"No books found for narrator: {name}")
    return books


@router.get("/narrator", response_model=list[dict])
async def search_db_narrators(
    name: Annotated[str, Query(description="Narrator name to search for")],
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page (max 100)")] = 20,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Search narrators by name from the local DB."""
    narrators = await search_narrators_from_db(session, name, limit=limit, page=page)
    if not narrators:
        raise NotFoundException(f"No narrators found matching: {name}")
    return narrators


@router.get("/series/{asin}/books", response_model=list[BookResponse])
async def get_db_series_books(
    asin: Annotated[str, Path(description="Series ASIN")],
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Get all books in a series from the local DB, sorted by position."""
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    books = await get_series_books_from_db(session, asin)
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