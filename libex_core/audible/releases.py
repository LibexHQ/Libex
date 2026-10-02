"""
Audible's release windows and genre taxonomy: the catalog walks behind
new releases and coming soon, the category taxonomy fetch, and the pure
helpers that go with them.

Audible has no new-releases or coming-soon endpoint, so both lists are
rebuilt from the catalog. Every catalog query is capped at roughly 535
results however it is filtered, and a parent category is not a superset of
its children, so a window is walked one category at a time, sorted by
release date, newest first. walk_catalog does that walk;
fetch_new_releases and fetch_coming_soon put a window's date gate on it
and sort the result. fetch_catalog_genres fetches the taxonomy those
category ids come from, flat, one row per node per parent, and
build_category_tree shapes that flat list for a response.

Nothing here touches a database, a cache, or the environment, and nothing
settles the books it returns: they leave as normalize_product made them,
tri-state flags and all, for the caller to store or settle as it needs.
Whether a window or a taxonomy is read from a cache before Audible is asked
is the caller's decision, made before it gets here. No message raised from
this module repeats anything a caller passed in.
"""

# Standard library
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

# Core
from libex_core.audible.books import (
    BOOK_RESPONSE_GROUPS,
    IMAGE_SIZES,
    filter_products,
    normalize_product,
)
from libex_core.audible.client import AudibleGet, validate_region
from libex_core.audible.search import SEARCH_PATH
from libex_core.models import CategoryAncestor, CategoryNode, FlatCategoryNode

CATEGORIES_PATH = "/1.0/catalog/categories"

# Audible's cap on one page of catalog results.
RELEASE_PAGE_SIZE = 50

# How many levels of the taxonomy one request asks for. The tree runs up to
# five deep.
GENRE_TAXONOMY_LEVELS = 5

DateGate = Callable[[datetime], bool]


# ============================================================
# PURE HELPERS
# ============================================================

def release_datetime(book: dict[str, Any]) -> datetime | None:
    """Parses a normalized book's releaseDate back into a datetime, or None."""
    raw = book.get("releaseDate")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def new_releases_gates(days: int, now: datetime) -> tuple[DateGate, DateGate]:
    """
    Returns (collect, should_stop) for the last `days` as of `now`.

    The walk is newest first: anything after `now` is a pre-order and is
    skipped, anything inside the window is kept, and the walk is over once it
    descends past the window's old edge.
    """
    window_start = now - timedelta(days=days)

    def collect(dt: datetime) -> bool:
        return window_start <= dt <= now

    def should_stop(dt: datetime) -> bool:
        return dt < window_start

    return collect, should_stop


def coming_soon_gates(days: int, now: datetime) -> tuple[DateGate, DateGate]:
    """
    Returns (collect, should_stop) for the next `days` as of `now`.

    The walk is newest first, so it starts beyond the window: anything past
    its far edge is skipped, anything inside is kept, and the walk is over
    once it descends to a title that is already out.
    """
    window_end = now + timedelta(days=days)

    def collect(dt: datetime) -> bool:
        return now < dt <= window_end

    def should_stop(dt: datetime) -> bool:
        return dt <= now

    return collect, should_stop


def new_releases_sort_key(book: dict[str, Any]) -> datetime:
    """Sort key for new releases, to be used with reverse=True: undated last."""
    return release_datetime(book) or datetime.min.replace(tzinfo=timezone.utc)


def coming_soon_sort_key(book: dict[str, Any]) -> datetime:
    """Sort key for coming soon, soonest first: undated last."""
    return release_datetime(book) or datetime.max.replace(tzinfo=timezone.utc)


def flatten_genre_nodes(data: dict[str, Any]) -> list[dict[str, str]]:
    """
    Flattens a taxonomy response into one row per node per parent.

    The taxonomy is a tree up to five levels deep and ragged -- some branches
    stop at two levels, some go five -- so this recurses to whatever depth
    Audible returned. A top-level node gets parent_id "" and every other node
    its parent's id. A node that sits under two parents yields one row per
    parent. Rows are deduped by (genre_id, parent_id) and carry genre_id,
    name and parent_id.
    """
    seen: set[tuple[str, str]] = set()
    nodes: list[dict[str, str]] = []

    def emit(node_list: list[dict], parent_id: str) -> None:
        for n in node_list:
            nid = n.get("id")
            name = n.get("name")
            if nid and name and (nid, parent_id) not in seen:
                seen.add((nid, parent_id))
                nodes.append({"genre_id": nid, "name": name, "parent_id": parent_id})
            if nid:
                emit(n.get("children", []), nid)

    emit(data.get("categories", []), "")
    return nodes


def build_category_tree(
    nodes: list[dict[str, str]],
    *,
    flat: bool = False,
    depth: int | None = None,
) -> list[CategoryNode] | list[FlatCategoryNode]:
    """
    Shapes flat taxonomy rows (as flatten_genre_nodes returns them) for a
    response, sorted by name at every level.

    By default the result is a nested tree, each node carrying its own
    children. With flat=True it is a flat list instead: every node at every
    level once per parent, carrying its ancestors root-first so its place in
    the taxonomy is still recoverable. `depth` limits how many levels come
    back (1 is the top level only); None means all of them.

    Raises ValueError if depth is given and is less than 1.
    """
    if depth is not None and depth < 1:
        raise ValueError("depth must be at least 1")

    # A node can sit under more than one parent, so it is keyed by parent in
    # the grouping, not globally. Both builders walk this one grouping from
    # the top-level roots (parent_id == "").
    by_parent: dict[str, list[dict[str, str]]] = {}
    for node in nodes:
        by_parent.setdefault(node.get("parent_id", ""), []).append(node)

    if flat:
        def build_flat(parent_id: str, ancestors: list[CategoryAncestor]) -> list[FlatCategoryNode]:
            # A node's level is its ancestor count + 1. It is emitted only
            # while within the depth limit, and descent stops once the next
            # level would exceed it.
            level = len(ancestors) + 1
            out: list[FlatCategoryNode] = []
            for n in sorted(by_parent.get(parent_id, []), key=lambda x: x["name"]):
                if depth is None or level <= depth:
                    out.append(
                        FlatCategoryNode(
                            id=n["genre_id"],
                            name=n["name"],
                            ancestors=ancestors,
                        )
                    )
                if depth is None or level < depth:
                    out.extend(
                        build_flat(
                            n["genre_id"],
                            ancestors + [CategoryAncestor(id=n["genre_id"], name=n["name"])],
                        )
                    )
            return out

        return build_flat("", [])

    def build(parent_id: str, level: int = 1) -> list[CategoryNode]:
        # Children are built only while a deeper level is still within the
        # depth limit; otherwise they come back empty.
        return sorted(
            (
                CategoryNode(
                    id=n["genre_id"],
                    name=n["name"],
                    children=(
                        build(n["genre_id"], level + 1)
                        if depth is None or level < depth
                        else []
                    ),
                )
                for n in by_parent.get(parent_id, [])
            ),
            key=lambda c: c.name,
        )

    return build("")


# ============================================================
# FETCHING
# ============================================================

async def fetch_catalog_genres(get: AudibleGet, region: str) -> list[dict[str, str]]:
    """
    Fetches one region's genre taxonomy, through `get`, and returns it
    flattened (see flatten_genre_nodes).

    A NotFoundException or AudibleAPIException from `get` propagates as it is.
    """
    region = validate_region(region)
    data = await get(
        region,
        CATEGORIES_PATH,
        {"root": "Genres", "categories_num_levels": GENRE_TAXONOMY_LEVELS},
    )
    return flatten_genre_nodes(data)


async def walk_catalog(
    get: AudibleGet,
    region: str,
    category_id: str | None,
    collect: DateGate,
    should_stop: DateGate,
) -> list[dict[str, Any]]:
    """
    Walks one catalog query in one region, through `get`, sorted by
    -ReleaseDate, and returns the normalized books `collect` accepted,
    deduped by ASIN.

    With a category_id the walk is scoped to that one category; without one it
    is the un-categoried catalog, which Audible caps at ~535 results, so it is
    a slice, not the full set. `collect(dt)` decides whether a book is in
    the window and `should_stop(dt)` whether the descending walk has passed
    its near edge. The walk ends at the first book that satisfies
    should_stop, when a page repeats the one before it (Audible's wall:
    once it runs out it repeats the last page), or when a page comes back
    short or empty. Books with no parseable date are skipped.

    A NotFoundException or AudibleAPIException from `get` propagates as it is.
    """
    region = validate_region(region)
    collected: dict[str, dict[str, Any]] = {}
    page = 0
    prev_asins: list[str] | None = None
    while True:
        params: dict[str, Any] = {
            "num_results": RELEASE_PAGE_SIZE,
            "page": page,
            "response_groups": BOOK_RESPONSE_GROUPS,
            "image_sizes": IMAGE_SIZES,
            "products_sort_by": "-ReleaseDate",
        }
        if category_id:
            params["category_id"] = category_id
        data = await get(region, SEARCH_PATH, params)
        products = filter_products(data.get("products", []))
        if not products:
            break

        page_asins = [p.get("asin") for p in products]
        if page_asins == prev_asins:
            break
        prev_asins = page_asins

        stop = False
        for product in products:
            book = normalize_product(product, region)
            dt = release_datetime(book)
            if dt is None:
                continue
            if should_stop(dt):
                stop = True
                break
            if collect(dt):
                asin = book.get("asin")
                if asin:
                    collected[asin] = book

        if stop:
            break
        if len(products) < RELEASE_PAGE_SIZE:
            break
        page += 1

    return list(collected.values())


async def fetch_new_releases(
    get: AudibleGet,
    region: str,
    days: int = 30,
    category: str | None = None,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """
    Returns the books released in the last `days` in one region, newest
    first, walked live through `get`. Pre-orders are skipped. `now` defaults
    to the current UTC time.

    The books are unsettled. Raises ValueError if days is less than 1. A
    NotFoundException or AudibleAPIException from `get` propagates as it is.
    """
    region = validate_region(region)
    if days < 1:
        raise ValueError("days must be at least 1")
    collect, should_stop = new_releases_gates(days, now or datetime.now(timezone.utc))
    books = await walk_catalog(get, region, category, collect, should_stop)
    books.sort(key=new_releases_sort_key, reverse=True)
    return books


async def fetch_coming_soon(
    get: AudibleGet,
    region: str,
    days: int = 30,
    category: str | None = None,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """
    Returns the books releasing in the next `days` in one region, soonest
    first, walked live through `get`. Titles already out are skipped. `now`
    defaults to the current UTC time.

    The books are unsettled. Raises ValueError if days is less than 1. A
    NotFoundException or AudibleAPIException from `get` propagates as it is.
    """
    region = validate_region(region)
    if days < 1:
        raise ValueError("days must be at least 1")
    collect, should_stop = coming_soon_gates(days, now or datetime.now(timezone.utc))
    books = await walk_catalog(get, region, category, collect, should_stop)
    books.sort(key=coming_soon_sort_key)
    return books
