"""
Authors router.
Endpoints for fetching author metadata, books, and search.
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
from app.api.routes.authors.schemas import AuthorResponse, AuthorBooksResponse

# Services
from app.services.audible.authors import (
    get_author,
    get_author_books,
    get_author_books_by_name,
    search_authors,
)

# Core
from app.core.logging import get_logger
from app.core.middleware import is_valid_asin, valid_region
from app.core.exceptions import NotFoundException

logger = get_logger()

router = APIRouter(prefix="/author", tags=["Authors"])

# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/search", response_model=list[AuthorResponse])
async def search(
    name: Annotated[str, Query(description="Author name to search for")],
    region: str = Depends(valid_region),
    session: AsyncSession = Depends(get_session),
) -> list[AuthorResponse]:
    """Search for authors by name."""
    authors = await search_authors(name, region, session)
    return [AuthorResponse(**a) for a in authors]


@router.get("/books", response_model=AuthorBooksResponse)
async def get_books_by_author_name(
    name: Annotated[str, Query(description="Author name")],
    region: str = Depends(valid_region),
    session: AsyncSession = Depends(get_session),
) -> AuthorBooksResponse:
    """
    Get book ASINs by author name.
    Used when no author ASIN is available.
    Compatible with AudiMeta /author/books endpoint.
    """
    asins = await get_author_books_by_name(name, region, session)
    return AuthorBooksResponse(asin="", region=region, book_asins=asins, total=len(asins))


@router.get("/books/{asin}", response_model=AuthorBooksResponse)
async def get_books_by_author(
    asin: Annotated[str, Path(description="Author ASIN")],
    region: str = Depends(valid_region),
    cache: Annotated[bool, Query(description="Return cached data if available")] = False,
    session: AsyncSession = Depends(get_session),
) -> AuthorBooksResponse:
    """
    Get all book ASINs for an author by ASIN.
    Uses Android endpoint with continuation token pagination.
    Compatible with AudiMeta /author/books/:asin endpoint.
    """
    if not is_valid_asin(asin):
        raise NotFoundException(f"Invalid ASIN format: {asin}")
    asins = await get_author_books(asin, region, session, cache)
    return AuthorBooksResponse(asin=asin, region=region, book_asins=asins, total=len(asins))


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