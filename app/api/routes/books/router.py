"""
Books router.
Endpoints for fetching book metadata by ASIN, bulk ASINs, and chapters.
"""

# Standard library
from typing import Annotated

# Third party
from fastapi import APIRouter, Query, Path, Depends
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.session import get_session

# Routes
from app.api.routes.books.schemas import BookResponse, ChapterInfo

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
    """Get a single book by ASIN."""
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    data = await get_book_by_asin(asin, region, session, cache)
    return BookResponse(**data)


@router.get("/{asin}/chapters", response_model=ChapterInfo)
async def get_book_chapters(
    asin: Annotated[str, Path(description="Audible ASIN")],
    region: str = Depends(valid_region),
    session: AsyncSession = Depends(get_session),
) -> ChapterInfo:
    """Get chapter information for a book by ASIN."""
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    data = await get_chapters(asin, region, session)
    return ChapterInfo(**data)


@router.get("", response_model=list[BookResponse])
async def get_books_bulk(
    asins: Annotated[str, Query(description="Comma-separated ASINs, max 1000")],
    region: str = Depends(valid_region),
    cache: Annotated[bool, Query(description="Return cached data if available")] = False,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """
    Get multiple books by ASIN.
    Accepts a comma-separated list of up to 1000 ASINs.
    Automatically chunks requests to respect Audible's 50 ASIN limit.
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
    return [BookResponse(**book) for book in data]