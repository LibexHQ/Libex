"""
Series router.
Endpoints for fetching series metadata and books.
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
from app.api.routes.series.schemas import SeriesResponse, SeriesBooksResponse

# Services
from app.services.audible.series import get_series, get_series_books, search_series

# Core
from app.core.logging import get_logger
from app.core.middleware import is_valid_asin, valid_region
from app.core.exceptions import NotFoundException

logger = get_logger()

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
    """Search for series by name."""
    results = await search_series(name, region, session)
    return [SeriesResponse(**s) for s in results]


@router.get("/books/{asin}", response_model=SeriesBooksResponse)
async def get_books_by_series(
    asin: Annotated[str, Path(description="Series ASIN")],
    region: str = Depends(valid_region),
    cache: Annotated[bool, Query(description="Return cached data if available")] = False,
    session: AsyncSession = Depends(get_session),
) -> SeriesBooksResponse:
    """
    Get all book ASINs for a series, sorted by position.
    Compatible with AudiMeta /series/books/:asin endpoint.
    """
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    asins = await get_series_books(asin, region, session, cache)
    return SeriesBooksResponse(asin=asin, region=region, book_asins=asins, total=len(asins))


@router.get("/{asin}", response_model=SeriesResponse)
async def get_series_by_asin(
    asin: Annotated[str, Path(description="Series ASIN")],
    region: str = Depends(valid_region),
    cache: Annotated[bool, Query(description="Return cached data if available")] = False,
    session: AsyncSession = Depends(get_session),
) -> SeriesResponse:
    """
    Get series metadata by ASIN.
    Compatible with AudiMeta /series/:asin endpoint.
    """
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    data = await get_series(asin, region, session, cache)
    return SeriesResponse(**data)