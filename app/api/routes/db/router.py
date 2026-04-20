"""
Database query endpoints.
Query the local database for indexed books without hitting Audible.
Only returns books that have been fetched and stored previously.
"""

# Standard library
from typing import Annotated, Any

# Third party
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

# Local
from app.api.routes.books.schemas import BookResponse
from app.core.exceptions import NotFoundException
from app.db.session import get_session
from app.services.db.reader import search_books_from_db

router = APIRouter(prefix="/db", tags=["Database"])


@router.get("/book", response_model=list[BookResponse])
async def search_db_books(
    title: Annotated[str | None, Query(description="ILIKE search on title")] = None,
    subtitle: Annotated[str | None, Query(description="ILIKE search on subtitle")] = None,
    region: Annotated[str | None, Query(description="Exact match on region")] = None,
    description: Annotated[str | None, Query(description="ILIKE search on description")] = None,
    summary: Annotated[str | None, Query(description="ILIKE search on summary")] = None,
    publisher: Annotated[str | None, Query(description="ILIKE search on publisher")] = None,
    copyright: Annotated[str | None, Query(description="ILIKE search on copyright")] = None,
    isbn: Annotated[str | None, Query(description="ILIKE search on ISBN")] = None,
    author_name: Annotated[str | None, Query(description="ILIKE search on author name")] = None,
    series_name: Annotated[str | None, Query(description="ILIKE search on series name")] = None,    
    language: Annotated[str | None, Query(description="Exact match on language")] = None,
    rating_better_than: Annotated[float | None, Query(description="Rating >= value")] = None,
    rating_worse_than: Annotated[float | None, Query(description="Rating <= value")] = None,
    longer_than: Annotated[int | None, Query(description="Length in minutes >= value")] = None,
    shorter_than: Annotated[int | None, Query(description="Length in minutes <= value")] = None,
    explicit: Annotated[bool | None, Query(description="Filter by explicit flag")] = None,
    whisper_sync: Annotated[bool | None, Query(description="Filter by whisper_sync flag")] = None,
    has_pdf: Annotated[bool | None, Query(description="Filter by has_pdf flag")] = None,
    book_format: Annotated[str | None, Query(description="Exact match on book_format")] = None,
    content_type: Annotated[str | None, Query(description="Exact match on content_type")] = None,
    content_delivery_type: Annotated[str | None, Query(description="Exact match on content_delivery_type")] = None,
    is_listenable: Annotated[bool | None, Query(description="Filter by is_listenable flag")] = None,
    is_buyable: Annotated[bool | None, Query(description="Filter by is_buyable flag")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page (max 100)")] = 20,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    filter_params = [
        title, subtitle, region, description, summary, publisher, copyright,
        isbn, author_name, series_name, language, rating_better_than, rating_worse_than, longer_than,
        shorter_than, explicit, whisper_sync, has_pdf, book_format,
        content_type, content_delivery_type, is_listenable, is_buyable,
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
        limit=limit,
        page=page,
    )

    if not books:
        raise NotFoundException("No books found matching the given parameters")

    return books