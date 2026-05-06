"""
Narrators router.
Endpoints for fetching books by narrator name.
Audible does not expose narrator profiles, ASINs, or bios —
narrators are name-only in Audible's data model.
"""

# Standard library
from typing import Annotated, Any

# Third party
from fastapi import APIRouter, Query, Depends
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.session import get_session

# Routes
from app.api.routes.books.schemas import BookResponse

# Services
from app.services.audible.search import search

# Core
from app.core.exceptions import NotFoundException
from app.core.middleware import valid_region

router = APIRouter(prefix="/narrator", tags=["Narrators"])


@router.get("/books", response_model=list[BookResponse])
async def get_narrator_books(
    name: Annotated[str, Query(description="Narrator name")],
    region: str = Depends(valid_region),
    limit: Annotated[int, Query(ge=1, le=50, description="Results per page (max 50)")] = 10,
    cache: Annotated[bool, Query(description="Return cached data if available")] = False,
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
    )
    if not results:
        raise NotFoundException(f"No books found for narrator: {name}")
    return results