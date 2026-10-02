"""
New releases, coming soon and the category taxonomy they are scoped by.

The library's counterparts of the hosted routes' live path, without the cache,
the database and persistence. Audible has no release endpoint, so a window is
rebuilt from the catalog by one walk, sorted by release date, over the
un-categoried catalog or over the one category given. The un-categoried walk
is capped by Audible at a few hundred results, so without a category the
result is a live sample, not the full catalog; a category scopes the walk and
returns the window in full for it. Nothing here fans out across categories on
its own: that is the caller's to do, with the ids categories returns.

A window that holds nothing is NotFoundException, as on the hosted routes, and
a walk Audible could not complete is AudibleAPIException: an outage is never
reported as an empty window. Nothing here reads the environment.
"""

# Standard library
import logging
import re
import time
from typing import Any

# Core
from libex_core.audible.books import settle_flags_list
from libex_core.audible.client import (
    AudibleGet,
    as_audible_failure,
    upstream_status_of,
    validate_region,
)
from libex_core.audible.releases import (
    build_category_tree,
    fetch_catalog_genres,
    fetch_coming_soon,
    fetch_new_releases,
)
from libex_core.exceptions import NotFoundException
from libex_core.lookup._shaping import check_shaping, shape_books
from libex_core.models import BookResponse, CategoryNode, FlatCategoryNode

logger = logging.getLogger("libex")

# The look-back and look-ahead windows, in days, the hosted routes offer.
RELEASE_WINDOWS = (30, 60, 90, 120, 240, 365)

# Audible category ids are purely numeric.
_CATEGORY_ID_PATTERN = re.compile(r"^\d{1,12}$")


def _check_window(days: int, category: str | None) -> None:
    """Raises ValueError for a window outside RELEASE_WINDOWS or a category
    that is not a numeric Audible category id. Neither message repeats the
    value."""
    if isinstance(days, bool) or days not in RELEASE_WINDOWS:
        raise ValueError(
            "days must be one of " + ", ".join(str(d) for d in RELEASE_WINDOWS)
        )
    if category is not None and not (
        isinstance(category, str) and _CATEGORY_ID_PATTERN.fullmatch(category)
    ):
        raise ValueError("category must be a numeric Audible category id")


async def _scan(
    get: AudibleGet,
    walk: Any,
    label: str,
    region: str,
    days: int,
    category: str | None,
    not_found_message: str,
) -> list[dict[str, Any]]:
    """Runs one release walk and returns its books settled; NotFoundException
    when the window held none, AudibleAPIException when the walk failed."""
    try:
        start = time.monotonic()
        books = await walk(get, region, days, category)
        took = round((time.monotonic() - start) * 1000, 2)
    except NotFoundException:
        # Audible answered that there is nothing, which is not an outage.
        raise NotFoundException(not_found_message) from None
    except Exception as e:
        logger.error(f"{label} scan failed", extra={
            "region": region,
            "category": category,
            "error_type": type(e).__name__,
            "upstream_status": upstream_status_of(e),
        })
        raise as_audible_failure(e, f"Audible {label.lower()} scan failed") from e

    logger.info(f"Requested Audible {label.lower()}", extra={
        "region": region,
        "days": days,
        "category": category,
        "results": len(books),
        "took": took,
    })
    return settle_flags_list(books)


async def new_releases(
    get: AudibleGet,
    days: int = 30,
    category: str | None = None,
    *,
    region: str = "us",
    filters: dict[str, Any] | None = None,
    sort: str | None = None,
    order: str = "desc",
) -> list[BookResponse]:
    """
    Books released in the last `days`, newest first, scanned live from Audible.
    Pre-orders are skipped.

    days is one of RELEASE_WINDOWS. category is an Audible category id from
    categories; without one the result is a live sample, not the full catalog.
    filters, sort and order are applied as on the hosted route, after the scan
    and only when given (the default order, desc, matters only with a sort);
    see libex_core.shaping for the filter names and sortable fields. A value
    outside what is allowed is ValueError, before anything is sent.

    Raises NotFoundException when no book is left, AudibleAPIException when
    the scan failed, and RegionException for an unknown region.
    """
    region = validate_region(region)
    _check_window(days, category)
    check_shaping(filters, sort, order)
    books = await _scan(
        get, fetch_new_releases, "New releases", region, days, category,
        "No new releases found",
    )
    books = shape_books(books, filters, sort, order)
    if not books:
        raise NotFoundException("No new releases found")
    return [BookResponse(**b) for b in books]


async def coming_soon(
    get: AudibleGet,
    days: int = 30,
    category: str | None = None,
    *,
    region: str = "us",
    filters: dict[str, Any] | None = None,
    sort: str | None = None,
    order: str = "asc",
) -> list[BookResponse]:
    """
    Books releasing in the next `days`, soonest first, scanned live from
    Audible. Titles already out are skipped.

    Otherwise as new_releases.
    """
    region = validate_region(region)
    _check_window(days, category)
    check_shaping(filters, sort, order)
    books = await _scan(
        get, fetch_coming_soon, "Coming soon", region, days, category,
        "No upcoming releases found",
    )
    books = shape_books(books, filters, sort, order)
    if not books:
        raise NotFoundException("No upcoming releases found")
    return [BookResponse(**b) for b in books]


async def categories(
    get: AudibleGet,
    *,
    region: str = "us",
    flat: bool = False,
    depth: int | None = None,
) -> list[CategoryNode] | list[FlatCategoryNode]:
    """
    Audible's genre categories for a region, the valid `category` values for
    new_releases and coming_soon.

    The taxonomy runs up to five levels deep and is ragged. By default it is a
    nested tree; flat=True returns every node at every level once per parent,
    each carrying its ancestors root-first. depth limits how many levels come
    back (1 is the top level only) and composes with flat; less than 1 is
    ValueError, before anything is sent. Fetched live, every call.

    Raises NotFoundException when the taxonomy is empty, AudibleAPIException
    when Audible could not be reached, and RegionException for an unknown
    region.
    """
    region = validate_region(region)
    if depth is not None and (isinstance(depth, bool) or not isinstance(depth, int) or depth < 1):
        raise ValueError("depth must be an integer of at least 1")
    try:
        start = time.monotonic()
        nodes = await fetch_catalog_genres(get, region)
        took = round((time.monotonic() - start) * 1000, 2)
    except NotFoundException:
        raise NotFoundException("No categories available") from None
    except Exception as e:
        logger.warning("Genre taxonomy fetch failed", extra={
            "region": region,
            "error_type": type(e).__name__,
            "upstream_status": upstream_status_of(e),
        })
        raise as_audible_failure(e, "Audible genre taxonomy fetch failed") from e

    logger.info("Requested Audible categories", extra={
        "region": region,
        "nodes": len(nodes),
        "took": took,
    })
    if not nodes:
        raise NotFoundException("No categories available")
    return build_category_tree(nodes, flat=flat, depth=depth)
