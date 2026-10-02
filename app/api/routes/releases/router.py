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
from app.api.routes.audible_outage import outage_as_unavailable
from app.api.routes.errors import ERROR_RESPONSES
from app.api.routes.sort_params import BookSortField, SortOrder
from app.api.routes.filter_params import LiveBookFilters
from app.api.routes.release_params import ReleaseWindow

# Services
from app.services.audible.releases import get_new_releases, get_coming_soon, ensure_genres
from app.services.sorting import sort_dicts, BOOK_SORT_FIELDS
from app.services.filtering import filter_dicts

# Core
from app.core.middleware import valid_region
from libex_core.exceptions import NotFoundException
from libex_core.audible.releases import build_category_tree
from libex_core.models import BookResponse, CategoryNode, FlatCategoryNode

router = APIRouter(tags=["Releases"])

# Audible category ids are purely numeric (see the stored Genre.asin column,
# String(12)); this keeps the param from carrying arbitrary text into the
# cache key and the Audible request.
_CATEGORY_ID_PATTERN = r"^\d{1,12}$"


# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/new-releases", response_model=list[BookResponse], responses=ERROR_RESPONSES)
async def new_releases(
    region: str = Depends(valid_region),
    days: Annotated[ReleaseWindow, Query(description="Look-back window in days")] = ReleaseWindow.days_30,
    category: Annotated[str | None, Query(pattern=_CATEGORY_ID_PATTERN, description="Audible category id to scope the scan to (see GET /categories). Without it, the scan returns a live sample, not the full catalog.")] = None,
    filters: LiveBookFilters = Depends(),
    sort: Annotated[BookSortField | None, Query(description="Field to sort the returned books by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.desc,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """
    Recently released books from the last N days, scanned live from Audible,
    newest first. Cached until the next UTC midnight. Returns 404 if none found,
    or 503 with Retry-After if Audible can't be reached (code
    `upstream_unavailable`).

    Pass a `category` id (from GET /categories) to scope the scan to one category
    and get the full window for it. Without a category, the scan walks Audible's
    un-categoried catalog, which it caps at a few hundred results — so the bare
    call returns a live sample, not the whole catalog. For the complete set, query
    a category, or use the DB endpoint /db/new-releases (kept current by the
    seeder), or aggregate per-category calls client-side.
    """
    books = await outage_as_unavailable(
        get_new_releases(region, session, days.value, category),
        "No new releases found",
    )
    books = filter_dicts(books, filters.as_kwargs())
    if sort is not None:
        books = sort_dicts(books, sort.value, order.value, BOOK_SORT_FIELDS)
    if not books:
        raise NotFoundException("No new releases found")
    return [BookResponse(**b) for b in books]


@router.get("/coming-soon", response_model=list[BookResponse], responses=ERROR_RESPONSES)
async def coming_soon(
    region: str = Depends(valid_region),
    days: Annotated[ReleaseWindow, Query(description="Look-ahead window in days")] = ReleaseWindow.days_30,
    category: Annotated[str | None, Query(pattern=_CATEGORY_ID_PATTERN, description="Audible category id to scope the scan to (see GET /categories). Without it, the scan returns a live sample, not the full catalog.")] = None,
    filters: LiveBookFilters = Depends(),
    sort: Annotated[BookSortField | None, Query(description="Field to sort the returned books by")] = None,
    order: Annotated[SortOrder, Query(description="Sort direction")] = SortOrder.asc,
    session: AsyncSession = Depends(get_session),
) -> list[BookResponse]:
    """
    Upcoming books releasing in the next N days, scanned live from Audible,
    soonest first. Cached until the next UTC midnight. Returns 404 if none found,
    or 503 with Retry-After if Audible can't be reached (code
    `upstream_unavailable`).

    Pass a `category` id (from GET /categories) to scope the scan to one category
    and get the full window for it. Without a category, the scan walks Audible's
    un-categoried catalog, which it caps at a few hundred results — so the bare
    call returns a live sample, not the whole catalog. For the complete set, query
    a category, or use the DB endpoint /db/coming-soon (kept current by the
    seeder), or aggregate per-category calls client-side.
    """
    books = await outage_as_unavailable(
        get_coming_soon(region, session, days.value, category),
        "No upcoming releases found",
    )
    books = filter_dicts(books, filters.as_kwargs())
    if sort is not None:
        books = sort_dicts(books, sort.value, order.value, BOOK_SORT_FIELDS)
    if not books:
        raise NotFoundException("No upcoming releases found")
    return [BookResponse(**b) for b in books]


@router.get("/categories", response_model=list[CategoryNode] | list[FlatCategoryNode], responses=ERROR_RESPONSES)
async def categories(
    region: str = Depends(valid_region),
    flat: Annotated[bool, Query(description="Return a flat list instead of a nested tree. Each node carries its full ancestry (root-first) so its place in the taxonomy is still recoverable.")] = False,
    depth: Annotated[int | None, Query(ge=1, description="Limit the response to this many levels. depth=1 returns only the top-level categories, depth=2 the top two levels, and so on. Omit for the full tree.")] = None,
    session: AsyncSession = Depends(get_session),
) -> list[CategoryNode] | list[FlatCategoryNode]:
    """
    Lists Audible's genre categories for a region — the valid `category` values
    for the /new-releases and /coming-soon scans.

    The taxonomy runs up to five levels deep and is ragged: a branch ends wherever
    Audible stops nesting. By default the response is a nested tree — each node
    carries its own `children`. Pass `flat=true` for a flat list instead: every
    node at every level as a single entry carrying its `ancestors` — the chain of
    {id, name} from the top-level root down to its immediate parent, in order — so
    a node's depth and lineage are still recoverable without walking a tree.

    Pass `depth=N` to limit how many levels come back: `depth=1` returns just the
    top-level categories (the parents), `depth=2` the top two levels, and so on.
    This works with both the nested and flat forms, and composes with `flat`.

    A node can sit under more than one parent; in the flat list it appears once
    per parent, each with that placement's own ancestry. These are the ids you
    pass as `category`, distinct from /db/genres (the genre/tag *names* attached
    to stored books). The list is read from the local cache of the taxonomy,
    which is only refreshed from Audible once the stored copy is more than a day
    old — a request within that window is served from the store without
    contacting Audible. A refresh reconciles the stored tree against Audible's
    current one: new nodes are added, existing ones updated, and placements that
    no longer exist are pruned — unless the fetch comes back suspiciously small,
    in which case it's treated as partial and only added to, never pruned, so a
    transient glitch can't wipe out real branches. Returns 404 if the taxonomy
    is empty, or 503 with Retry-After if Audible can't be reached and nothing
    is stored (code `upstream_unavailable`).
    """
    nodes = await outage_as_unavailable(
        ensure_genres(session, region),
        "No categories available",
    )
    if not nodes:
        raise NotFoundException("No categories available")

    return build_category_tree(nodes, flat=flat, depth=depth)
