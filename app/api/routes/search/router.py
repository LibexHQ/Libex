"""
Search router.
Endpoints for searching the Audible catalog.
Compatible with AudiMeta endpoint structure for drop-in replacement.
"""

# Standard library
from typing import Annotated

# Third party
from fastapi import APIRouter, Query, Depends
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.session import get_session

# Routes
from app.api.routes.books.schemas import BookResponse

# Services
from app.services.audible.search import search, quick_search

# Core
from app.core.logging import get_logger
from app.core.middleware import valid_region

logger = get_logger()

router = APIRouter(tags=["Search"])

# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/search", response_model=list[BookResponse])
async def search_books(
    region: str = Depends(valid_region),
    title: Annotated[str | None, Query(description="Book title")] = None,
    author: Annotated[str | None, Query(description="Author name")] = None,
    keywords: Annotated[str | None, Query(description="Keywords")] = None,
    limit: Annotated[int, Query(description="Maximum results", ge=1, le=50)] = 10,
    cache: Annotated[bool, Query(description="Return cached data if available")] = False,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """
    Search the Audible catalog.
    Accepts title, author, keywords or any combination.
    Compatible with AudiMeta /search endpoint.
    """
    books = await search(region, session, title, author, keywords, limit)
    return [BookResponse(**b) for b in books]


@router.get("/quick-search", response_model=list[BookResponse])
async def quick_search_books(
    keywords: Annotated[str, Query(description="Search keywords")],
    region: str = Depends(valid_region),
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """
    Quick search using Audible search suggestions.
    Compatible with AudiMeta /quick-search endpoint.
    """
    books = await quick_search(keywords, region, session)
    return [BookResponse(**b) for b in books]