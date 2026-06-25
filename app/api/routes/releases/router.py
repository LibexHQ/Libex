"""
Releases router.
Live new-releases and coming-soon endpoints, scanned fresh from Audible and
cached until the next UTC midnight.
"""

# Standard library
from typing import Annotated

# Third party
from fastapi import APIRouter, Query, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.session import get_session

# Routes
from app.api.routes.books.schemas import BookResponse
from app.api.routes.sort_params import BookSortField, SortOrder
from app.api.routes.filter_params import LiveBookFilters
from app.api.routes.release_params import ReleaseWindow

# Services
from app.services.audible.releases import get_new_releases, get_coming_soon, _ensure_genres
from app.services.sorting import sort_dicts, BOOK_SORT_FIELDS
from app.services.filtering import filter_dicts

# Core
from app.core.middleware import valid_region
from app.core.exceptions import NotFoundException

router = APIRouter(tags=["Releases"])


class CategoryNode(BaseModel):
    id: str
    name: str
    children: list["CategoryNode"] = []


# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/new-releases", response_model=list[BookResponse])
async def new_releases(
    region: str = Depends(valid_region),
    days: Annotated[ReleaseWindow, Query(description="Look-back window in days")] = ReleaseWindow.days_30,
    category: Annotated[str | None, Query(description="Audible category id to scope the scan to (see GET /categories). Without it, the scan returns a live sample, not the full catalog.")] = None,
    filters: LiveBookFilters = Depends(),
    sort: Annotated[BookSortField | None, Query(description="Field to sort the returned books by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.desc,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """
    Recently released books from the last N days, scanned live from Audible,
    newest first. Cached until the next UTC midnight. Returns 404 if none found.

    Pass a `category` id (from GET /categories) to scope the scan to one category
    and get the full window for it. Without a category, the scan walks Audible's
    un-categoried catalog, which it caps at a few hundred results — so the bare
    call returns a live sample, not the whole catalog. For the complete set, query
    a category, or use the DB endpoint /db/new-releases (kept current by the
    seeder), or aggregate per-category calls client-side.
    """
    books = await get_new_releases(region, session, days.value, category)
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
    category: Annotated[str | None, Query(description="Audible category id to scope the scan to (see GET /categories). Without it, the scan returns a live sample, not the full catalog.")] = None,
    filters: LiveBookFilters = Depends(),
    sort: Annotated[BookSortField | None, Query(description="Field to sort the returned books by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """
    Upcoming books releasing in the next N days, scanned live from Audible,
    soonest first. Cached until the next UTC midnight. Returns 404 if none found.

    Pass a `category` id (from GET /categories) to scope the scan to one category
    and get the full window for it. Without a category, the scan walks Audible's
    un-categoried catalog, which it caps at a few hundred results — so the bare
    call returns a live sample, not the whole catalog. For the complete set, query
    a category, or use the DB endpoint /db/coming-soon (kept current by the
    seeder), or aggregate per-category calls client-side.
    """
    books = await get_coming_soon(region, session, days.value, category)
    books = filter_dicts(books, filters.as_kwargs())
    if sort is not None:
        books = sort_dicts(books, sort.value, order.value, BOOK_SORT_FIELDS)
    if not books:
        raise NotFoundException("No upcoming releases found")
    return [BookResponse(**b) for b in books]


@router.get("/categories", response_model=list[CategoryNode])
async def categories(
    region: str = Depends(valid_region),
    session: AsyncSession = Depends(get_session),
) -> list[CategoryNode]:
    """
    Lists Audible's genre categories for a region — the valid `category` values
    for the /new-releases and /coming-soon scans — as a nested tree.

    The taxonomy runs up to five levels deep and is ragged: each node carries its
    own `children`, and a branch ends wherever Audible stops nesting. These are
    the ids you pass as `category`, distinct from /db/genres (the genre/tag *names*
    attached to stored books). The list is read from the local cache of the
    taxonomy, refreshed from Audible at most once a day. Returns 404 if the
    taxonomy can't be loaded.
    """
    nodes = await _ensure_genres(session, region)
    if not nodes:
        raise NotFoundException("No categories available")

    # Group every node under its parent_id, then build the tree recursively from
    # the top-level roots (parent_id == ""). A node can appear under more than one
    # parent, so it's keyed by parent in the grouping, not globally.
    by_parent: dict[str, list[dict]] = {}
    for node in nodes:
        by_parent.setdefault(node.get("parent_id", ""), []).append(node)

    def build(parent_id: str) -> list[CategoryNode]:
        return sorted(
            (
                CategoryNode(
                    id=n["genre_id"],
                    name=n["name"],
                    children=build(n["genre_id"]),
                )
                for n in by_parent.get(parent_id, [])
            ),
            key=lambda c: c.name,
        )

    return build("")