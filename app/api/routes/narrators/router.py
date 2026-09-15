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
from app.api.routes.audible_outage import outage_as_not_found
from app.api.routes.cache_param import CacheInertParam, apply_cache_control

# Services
from app.services.audible.search import search

# Core
from libex_core.exceptions import NotFoundException
from libex_core.models import BookResponse
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
    not_found_message = f"No books found for narrator: {name}"
    results = await outage_as_not_found(
        search(
            region=region,
            session=session,
            narrator=name,
            limit=limit,
            page=page,
        ),
        not_found_message,
    )
    if not results:
        raise NotFoundException(not_found_message)
    apply_cache_control(response, cache)
    return results
