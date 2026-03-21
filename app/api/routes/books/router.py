"""
Books router.
Endpoints for fetching book metadata by ASIN, bulk ASINs, and chapters.
Response formats match AudiMeta exactly for drop-in compatibility.
"""

# Standard library
from typing import Annotated

# Third party
from fastapi import APIRouter, Query, Path, Depends
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.session import get_session

# Routes
from app.api.routes.books.schemas import BookResponse, BulkBookResponse, ChapterResponse

# Services
from app.services.audible.books import get_book_by_asin, get_books_by_asins, get_chapters

# Core
from app.core.exceptions import NotFoundException
from app.core.logging import get_logger
from app.core.middleware import is_valid_asin, valid_region

logger = get_logger()

router = APIRouter(prefix="/book", tags=["Books"])

# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/{asin}", response_model=BookResponse)
async def get_book(
    asin: Annotated[str, Path(description="Audible ASIN")],
    region: str = Depends(valid_region),
    cache: Annotated[bool, Query(description="Return cached data if available")] = False,
    session: AsyncSession = Depends(get_session),
) -> BookResponse:
    """
    Get a single book by ASIN.
    Returns a single book object directly.
    """
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    data = await get_book_by_asin(asin, region, session, cache)
    return BookResponse(**data)


@router.get("/{asin}/chapters", response_model=ChapterResponse)
async def get_book_chapters(
    asin: Annotated[str, Path(description="Audible ASIN")],
    region: str = Depends(valid_region),
    session: AsyncSession = Depends(get_session),
) -> ChapterResponse:
    """Get chapter information for a book by ASIN."""
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    data = await get_chapters(asin, region, session)
    return ChapterResponse(**data)


@router.get("/chapters/{asin}", response_model=ChapterResponse, include_in_schema=False)
async def get_book_chapters_legacy(
    asin: Annotated[str, Path(description="Audible ASIN")],
    region: str = Depends(valid_region),
    session: AsyncSession = Depends(get_session),
) -> ChapterResponse:
    """Legacy endpoint. Use /book/{asin}/chapters instead."""
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    data = await get_chapters(asin, region, session)
    return ChapterResponse(**data)


@router.get("", response_model=BulkBookResponse)
async def get_books_bulk(
    asins: Annotated[str, Query(description="Comma-separated ASINs, max 1000")],
    region: str = Depends(valid_region),
    cache: Annotated[bool, Query(description="Return cached data if available")] = False,
    session: AsyncSession = Depends(get_session),
) -> BulkBookResponse:
    """
    Get multiple books by ASIN.
    Returns {"books": [...], "notFound": [...]} matching AudiMeta's bulk format.
    """
    asin_list = [a.strip() for a in asins.split(",") if a.strip()]

    invalid = [a for a in asin_list if not is_valid_asin(a)]
    if invalid:
        raise NotFoundException(f"Invalid ASIN format: {', '.join(invalid)}")

    if not asin_list:
        raise NotFoundException("No valid ASINs provided")

    if len(asin_list) > 1000:
        raise NotFoundException("Maximum 1000 ASINs per request")

    data = await get_books_by_asins(asin_list, region, session, cache)

    found_asins = {book["asin"] for book in data}
    not_found = [a for a in asin_list if a not in found_asins]

    return BulkBookResponse(
        books=[BookResponse(**book) for book in data],
        notFound=not_found,
    )