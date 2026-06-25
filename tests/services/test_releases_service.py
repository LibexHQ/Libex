"""
Service tests for the live new-releases and coming-soon endpoints.

The live endpoints now walk a SINGLE catalog query per request: scoped to one
category when a `category` id is given, or the un-categoried catalog when not
(the bare-call "sample"). The all-genres fan-out lives only in the seeder. These
tests mock Audible (audible_get), the cache, and the background persist so we
exercise the windowing, the duplicate-page wall, the sorts, the cache-first
short-circuit, the category scoping, and the cache-key-per-category behavior —
without real HTTP or DB.

The genre taxonomy helper (_fetch_catalog_genres) flattens every node at every
level with its parent_id, feeding the /categories discovery endpoint; it's
covered here at the unit level.
"""

# Standard library
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Services
from app.services.audible import releases


NOW = datetime.now(timezone.utc)


def _product(asin, days_offset):
    """Builds a minimal Audible catalog product with a release date offset from now."""
    dt = NOW + timedelta(days=days_offset)
    return {
        "asin": asin,
        "title": f"Book {asin}",
        "release_date": dt.strftime("%Y-%m-%d"),
        "publication_datetime": dt.isoformat(),
    }


def _page(*products):
    return {"products": list(products)}


def _catnode(spec):
    """
    Builds one taxonomy node from a spec that is either (id, name) for a node
    with no children, or (id, name, [child_specs]) for a node with children.
    Recurses, so child specs can themselves carry children to any depth.
    """
    if len(spec) == 2:
        nid, name = spec
        kids = []
    else:
        nid, name, kids = spec
    return {"id": nid, "name": name, "children": [_catnode(k) for k in kids]}


def _categories(*parents):
    """
    Builds a /catalog/categories taxonomy. Each top-level parent is a spec
    (id, name) or (id, name, [child_specs]); child specs nest to any depth, so
    this can build the full multi-level tree Audible returns.
    """
    return {"categories": [_catnode(p) for p in parents]}


def _audible(pages=None, categories=None, categories_error=False):
    """
    Async audible_get stand-in. Serves the taxonomy on a /categories call and
    product pages on a /products call. Product pages are keyed by category_id;
    the un-categoried walk (no category_id) is keyed under None. Each value is a
    list of pages, served by page index, empty once exhausted.
    """
    pages = pages or {}

    def _get(region, path, params=None):
        params = params or {}
        if "/categories" in path:
            if categories_error:
                raise RuntimeError("categories unavailable")
            return categories if categories is not None else {"categories": []}
        gid = params.get("category_id")  # None for the un-categoried walk
        page = params.get("page", 0)
        these = pages.get(gid, [])
        return these[page] if page < len(these) else {"products": []}

    return AsyncMock(side_effect=_get)


# ============================================================
# CACHE-FIRST SHORT-CIRCUIT
# ============================================================

@pytest.mark.asyncio
async def test_new_releases_cache_hit_skips_audible():
    """A cache hit returns immediately without touching Audible."""
    cached = [{"asin": "B1", "title": "Cached"}]
    mock_get = _audible()
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=cached)), \
         patch.object(releases, "audible_get", new=mock_get):
        result = await releases.get_new_releases("us", AsyncMock(), days=30)
        assert result == cached
        mock_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_coming_soon_cache_hit_skips_audible():
    """A cache hit returns immediately without touching Audible."""
    cached = [{"asin": "B1", "title": "Cached"}]
    mock_get = _audible()
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=cached)), \
         patch.object(releases, "audible_get", new=mock_get):
        result = await releases.get_coming_soon("us", AsyncMock(), days=30)
        assert result == cached
        mock_get.assert_not_awaited()


# ============================================================
# WINDOWING (single walk)
# ============================================================

@pytest.mark.asyncio
async def test_new_releases_windows_recent_past():
    """New releases collects books within the look-back window and stops below it."""
    page = _page(
        _product("BFUTURE", 10),   # future — skipped
        _product("BRECENT1", -5),  # in window
        _product("BRECENT2", -20),  # in window
        _product("BOLD", -40),     # below 30-day window — triggers stop
    )
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=_audible({"C1": [page]})):
        result = await releases.get_new_releases("us", AsyncMock(), days=30, category="C1")
        asins = [b["asin"] for b in result]
        assert asins == ["BRECENT1", "BRECENT2"]


@pytest.mark.asyncio
async def test_coming_soon_windows_future_and_sorts_soonest_first():
    """Coming soon collects upcoming books, stops at released, sorts soonest first."""
    page = _page(
        _product("BFAR", 100),     # beyond 30-day window — skipped
        _product("BLATER", 20),    # in window
        _product("BSOONER", 5),    # in window
        _product("BRELEASED", -1),  # already released — triggers stop
    )
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=_audible({"C1": [page]})):
        result = await releases.get_coming_soon("us", AsyncMock(), days=30, category="C1")
        asins = [b["asin"] for b in result]
        assert asins == ["BSOONER", "BLATER"]


@pytest.mark.asyncio
async def test_new_releases_sorted_newest_first():
    """The returned new releases are sorted newest-first regardless of page order."""
    page = _page(
        _product("BMID", -10),
        _product("BNEWEST", -1),
        _product("BOLDEST", -25),
    )
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=_audible({"C1": [page]})):
        result = await releases.get_new_releases("us", AsyncMock(), days=30, category="C1")
        asins = [b["asin"] for b in result]
        assert asins == ["BNEWEST", "BMID", "BOLDEST"]


# ============================================================
# NO CATEGORY -> SINGLE UN-CATEGORIED WALK (the "sample")
# ============================================================

@pytest.mark.asyncio
async def test_no_category_walks_uncategoried_catalog():
    """Without a category, the scan walks the un-categoried catalog (no category_id)."""
    page = _page(_product("B1", -2), _product("B2", -3))
    mock_get = _audible({None: [page]})  # pages served only when category_id is absent
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=mock_get):
        result = await releases.get_new_releases("us", AsyncMock(), days=30)
        asins = sorted(b["asin"] for b in result)
        assert asins == ["B1", "B2"]
        # No product call carried a category_id.
        product_calls = [c for c in mock_get.await_args_list if "/products" in c.args[1]]
        assert product_calls
        assert all("category_id" not in (c.args[2] or {}) for c in product_calls)


@pytest.mark.asyncio
async def test_category_walk_sends_category_id():
    """With a category, every product call is scoped to that category_id."""
    page = _page(_product("B1", -2))
    mock_get = _audible({"C1": [page]})
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=mock_get):
        await releases.get_new_releases("us", AsyncMock(), days=30, category="C1")
        product_calls = [c for c in mock_get.await_args_list if "/products" in c.args[1]]
        assert product_calls
        assert all((c.args[2] or {}).get("category_id") == "C1" for c in product_calls)


# ============================================================
# DUPLICATE-PAGE WALL
# ============================================================

@pytest.mark.asyncio
async def test_duplicate_page_wall_stops_walk():
    """A repeated page (Audible's ~535 wall) stops the walk; each book appears once."""
    full = _page(*[_product(f"B{i:02d}", -1) for i in range(50)])
    mock_get = _audible({"C1": [full, full]})  # same page twice
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=mock_get):
        result = await releases.get_new_releases("us", AsyncMock(), days=30, category="C1")
        assert len(result) == 50  # deduped, not 100
        product_calls = [c for c in mock_get.await_args_list if "/products" in c.args[1]]
        assert len(product_calls) == 2  # page 0, then the repeat -> stop


# ============================================================
# CACHE WRITE
# ============================================================

@pytest.mark.asyncio
async def test_new_releases_caches_with_midnight_ttl():
    """A non-empty result is cached with a TTL to the next UTC midnight."""
    page = _page(_product("B1", -2))
    mock_set = AsyncMock()
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=mock_set), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "seconds_until_utc_midnight", return_value=12345), \
         patch.object(releases, "audible_get", new=_audible({"C1": [page]})):
        await releases.get_new_releases("us", AsyncMock(), days=30, category="C1")
        assert mock_set.await_count == 1
        assert mock_set.await_args.kwargs.get("ttl_seconds") == 12345


@pytest.mark.asyncio
async def test_cache_key_varies_by_category():
    """The cache key includes the category, so bare and categoried calls differ."""
    bare = releases.cache.new_releases_key("us", 30, None)
    cat = releases.cache.new_releases_key("us", 30, "C1")
    assert bare != cat
    assert bare == releases.cache.new_releases_key("us", 30)  # None == omitted


@pytest.mark.asyncio
async def test_empty_scan_returns_empty_and_does_not_cache():
    """When the walk finds nothing, return empty and do not write the cache."""
    mock_set = AsyncMock()
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=mock_set), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=_audible({"C1": []})):
        result = await releases.get_new_releases("us", AsyncMock(), days=30, category="C1")
        assert result == []
        mock_set.assert_not_awaited()


# ============================================================
# TAXONOMY (all levels) — feeds /categories
# ============================================================

@pytest.mark.asyncio
async def test_fetch_catalog_genres_keeps_parents_and_leaves():
    """_fetch_catalog_genres returns parents (parent_id='') AND leaves (parent_id=<parent>)."""
    taxonomy = _categories(
        ("P1", "History", [("L1", "Ancient"), ("L2", "Modern")]),
        ("P2", "Sci-Fi", [("L3", "Space")]),
    )
    with patch.object(releases, "audible_get", new=_audible(categories=taxonomy)):
        nodes = await releases._fetch_catalog_genres("us")
    by_id = {(n["genre_id"], n["parent_id"]) for n in nodes}
    assert ("P1", "") in by_id
    assert ("P2", "") in by_id
    assert ("L1", "P1") in by_id
    assert ("L2", "P1") in by_id
    assert ("L3", "P2") in by_id
    assert len(nodes) == 5


@pytest.mark.asyncio
async def test_fetch_catalog_genres_dual_parent_leaf_kept_per_parent():
    """A leaf under two parents yields one node per parent."""
    taxonomy = _categories(
        ("P1", "History", [("LX", "Shared")]),
        ("P2", "Society", [("LX", "Shared")]),
    )
    with patch.object(releases, "audible_get", new=_audible(categories=taxonomy)):
        nodes = await releases._fetch_catalog_genres("us")
    lx = [n for n in nodes if n["genre_id"] == "LX"]
    assert {n["parent_id"] for n in lx} == {"P1", "P2"}


@pytest.mark.asyncio
async def test_fetch_catalog_genres_recurses_all_levels():
    """
    The taxonomy is up to five levels deep and ragged, so the flatten recurses to
    whatever depth Audible returns. Every node is captured with its parent's id —
    including a deep node whose parent is itself a grandchild — and a branch that
    ends early (a childless node) is handled without forcing further levels.
    """
    taxonomy = _categories(
        ("P1", "Arts", [
            ("C1", "Performing", [
                ("G1", "Film & TV", [
                    ("GG1", "Direction"),   # depth 4
                ]),
            ]),
            ("C2", "Architecture"),         # childless leaf (depth 2)
        ]),
        ("P2", "History"),                  # childless parent (depth 1)
    )
    with patch.object(releases, "audible_get", new=_audible(categories=taxonomy)):
        nodes = await releases._fetch_catalog_genres("us")
    by_id = {(n["genre_id"], n["parent_id"]) for n in nodes}
    assert ("P1", "") in by_id          # parent
    assert ("P2", "") in by_id          # childless parent
    assert ("C1", "P1") in by_id        # child
    assert ("C2", "P1") in by_id        # childless leaf
    assert ("G1", "C1") in by_id        # grandchild
    assert ("GG1", "G1") in by_id       # great-grandchild — recursion reached depth 4
    assert len(nodes) == 6