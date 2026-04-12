"""
Series router.
Compatible with AudiMeta endpoint structure for drop-in replacement.
"""

# Standard library
from typing import Annotated

# Third party
from fastapi import APIRouter, Query, Path, Depends
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.session import get_session

# Routes
from app.api.routes.series.schemas import SeriesResponse
from app.api.routes.books.schemas import BookResponse

# Services
from app.services.audible.series import get_series, get_series_books, search_series
from app.services.audible.books import get_books_by_asins

# Core
from app.core.middleware import is_valid_asin, valid_region
from app.core.exceptions import NotFoundException

router = APIRouter(prefix="/series", tags=["Series"])

# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/search", response_model=list[SeriesResponse])
async def search(
    name: Annotated[str, Query(description="Series name to search for")],
    region: str = Depends(valid_region),
    session: AsyncSession = Depends(get_session),
) -> list[SeriesResponse]:
    """Search for series by name. Returns 404 if none found."""
    results = await search_series(name, region, session)
    if not results:
        raise NotFoundException("No series found")
    return [SeriesResponse(**s) for s in results]


@router.get("", response_model=list[SeriesResponse], include_in_schema=False)
async def search_legacy(
    name: Annotated[str, Query(description="Series name to search for")],
    region: str = Depends(valid_region),
    session: AsyncSession = Depends(get_session),
) -> list[SeriesResponse]:
    """Legacy endpoint. Use /series/search instead."""
    results = await search_series(name, region, session)
    if not results:
        raise NotFoundException("No series found")
    return [SeriesResponse(**s) for s in results]


@router.get("/books/{asin}", response_model=list[BookResponse])
async def get_books_by_series(
    asin: Annotated[str, Path(description="Series ASIN")],
    region: str = Depends(valid_region),
    cache: Annotated[bool, Query(description="Return cached data if available")] = False,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """
    Get all books in a series sorted by position.
    Returns full book objects matching AudiMeta's BookDto format.
    """
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    asins = await get_series_books(asin, region, session, cache)
    if not asins:
        raise NotFoundException("No books found for series")
    books = await get_books_by_asins(asins, region, session)
    return [BookResponse(**b) for b in books]


@router.get("/{asin}/books", response_model=list[BookResponse], include_in_schema=False)
async def get_books_by_series_primary(
    asin: Annotated[str, Path(description="Series ASIN")],
    region: str = Depends(valid_region),
    cache: Annotated[bool, Query(description="Return cached data if available")] = False,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """Legacy endpoint. Use /series/books/{asin} instead."""
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    asins = await get_series_books(asin, region, session, cache)
    if not asins:
        raise NotFoundException("No books found for series")
    books = await get_books_by_asins(asins, region, session)
    return [BookResponse(**b) for b in books]


@router.get("/{asin}", response_model=SeriesResponse)
async def get_series_by_asin(
    asin: Annotated[str, Path(description="Series ASIN")],
    region: str = Depends(valid_region),
    cache: Annotated[bool, Query(description="Return cached data if available")] = False,
    session: AsyncSession = Depends(get_session),
) -> SeriesResponse:
    """Get series metadata by ASIN."""
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    data = await get_series(asin, region, session, cache)
    return SeriesResponse(**data)