"""
Releases router.
Live new-releases and coming-soon endpoints, scanned fresh from Audible and
cached until the next UTC midnight.
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
from app.api.routes.sort_params import BookSortField, SortOrder
from app.api.routes.filter_params import LiveBookFilters
from app.api.routes.release_params import ReleaseWindow

# Services
from app.services.audible.releases import get_new_releases, get_coming_soon
from app.services.sorting import sort_dicts, BOOK_SORT_FIELDS
from app.services.filtering import filter_dicts

# Core
from app.core.middleware import valid_region
from app.core.exceptions import NotFoundException

router = APIRouter(tags=["Releases"])


# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/new-releases", response_model=list[BookResponse])
async def new_releases(
    region: str = Depends(valid_region),
    days: Annotated[ReleaseWindow, Query(description="Look-back window in days")] = ReleaseWindow.days_30,
    filters: LiveBookFilters = Depends(),
    sort: Annotated[BookSortField | None, Query(description="Field to sort the returned books by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.desc,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """
    Recently released books from the last N days, scanned live from Audible,
    newest first. Cached until the next UTC midnight. Returns 404 if none found.
    """
    books = await get_new_releases(region, session, days.value)
    books = filter_dicts(books, filters.as_kwargs())
    if sort is not None:
        books = sort_dicts(books, sort.value, order.value, BOOK_SORT_FIELDS)
    if not books:
        raise NotFoundException("No new releases found")
    return [BookResponse(**b) for b in books]


@router.get("/coming-soon", response_model=list[BookResponse])
async def coming_soon(
    region: str = Depends(valid_region),
    days: Annotated[ReleaseWindow, Query(description="Look-ahead window in days")] = ReleaseWindow.days_30,
    filters: LiveBookFilters = Depends(),
    sort: Annotated[BookSortField | None, Query(description="Field to sort the returned books by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """
    Upcoming books releasing in the next N days, scanned live from Audible,
    soonest first. Cached until the next UTC midnight. Returns 404 if none found.
    """
    books = await get_coming_soon(region, session, days.value)
    books = filter_dicts(books, filters.as_kwargs())
    if sort is not None:
        books = sort_dicts(books, sort.value, order.value, BOOK_SORT_FIELDS)
    if not books:
        raise NotFoundException("No upcoming releases found")
    return [BookResponse(**b) for b in books]