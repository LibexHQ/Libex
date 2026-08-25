"""
Narrators router.
Endpoints for fetching books by narrator name.
Audible does not expose narrator profiles, ASINs, or bios —
narrators are name-only in Audible's data model.
"""

# Standard library
from typing import Annotated, Any

# Third party
from fastapi import APIRouter, Query, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.session import get_session

# Routes
from app.api.routes.books.schemas import BookResponse
from app.api.routes.cache_param import CacheInertParam, apply_cache_control

# Services
from app.services.audible.search import search

# Core
from app.core.exceptions import NotFoundException
from app.core.middleware import valid_region

router = APIRouter(prefix="/narrator", tags=["Narrators"])


@router.get("/books", response_model=list[BookResponse])
async def get_narrator_books(
    name: Annotated[str, Query(description="Narrator name")],
    response: Response,
    region: str = Depends(valid_region),
    limit: Annotated[int, Query(ge=1, le=50, description="Maximum results (max 50)")] = 10,
    # Audible's product listing has a result ceiling that returns a full page
    # under HTTP 200 rather than an error or an empty list, so a caller paging
    # until it sees an empty response would never stop. le=9 stops short of
    # that ceiling deliberately and conservatively, not at its measured edge.
    page: Annotated[int, Query(ge=0, le=9, description="Page number")] = 0,
    # Stays False by default and stays inert -- see cache_param.CacheInertParam.
    # This route has no cache to read from; search() always fetches live.
    cache: CacheInertParam = False,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """
    Get books by narrator name.
    Searches the Audible catalog by narrator and returns full book metadata.
    """
    results = await search(
        region=region,
        session=session,
        narrator=name,
        limit=limit,
        page=page,
    )
    if not results:
        raise NotFoundException(f"No books found for narrator: {name}")
    apply_cache_control(response, cache)
    return results
