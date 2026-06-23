"""
Service tests for the live new-releases and coming-soon endpoints.

The endpoints now reconstruct their lists by walking every LEAF genre's catalog
(category_id + -ReleaseDate), unioning and deduping across genres, then filtering
to the window and sorting. These tests mock Audible (audible_get), the genre
store (get_stored_genres / upsert_genres), the cache, and the background persist
so we exercise the windowing, the per-genre walk + duplicate-page wall, the
cross-genre union/dedupe, the sorts, the cache-first short-circuit, and the
inline genre-list refresh — without real HTTP or DB.
"""

# Standard library
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Services
from app.services.audible import releases

NOW = datetime.now(timezone.utc)

# A non-stale stored-genre tuple (genres + a just-now last_checked) so
# _ensure_genres short-circuits and the test drives only the catalog walk.
_FRESH_ONE = ([{"genre_id": "G1", "name": "G1"}], NOW)


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


def _empty():
    return {"products": []}


def _categories(*parents):
    """
    Builds a /catalog/categories taxonomy. Each parent is
    (parent_id, parent_name, [(leaf_id, leaf_name), ...]).
    """
    return {
        "categories": [
            {
                "id": pid,
                "name": pname,
                "children": [{"id": cid, "name": cname} for cid, cname in children],
            }
            for pid, pname, children in parents
        ]
    }


def _audible(pages_by_genre=None, categories=None, categories_error=False):
    """
    Async audible_get stand-in: serves the taxonomy for a /categories call and
    per-genre product pages keyed by category_id (empty once a genre's pages run
    out). Optionally raises on the /categories call to test the refresh-failure
    fallback.
    """
    pages_by_genre = pages_by_genre or {}

    def _get(region, path, params=None):
        params = params or {}
        if "/categories" in path:
            if categories_error:
                raise RuntimeError("categories unavailable")
            return categories if categories is not None else {"categories": []}
        gid = params.get("category_id")
        page = params.get("page", 0)
        pages = pages_by_genre.get(gid, [])
        return pages[page] if page < len(pages) else {"products": []}

    return AsyncMock(side_effect=_get)


# ============================================================
# CACHE-FIRST SHORT-CIRCUIT
# ============================================================

@pytest.mark.asyncio
async def test_new_releases_cache_hit_skips_audible():
    """A cache hit returns immediately without scanning Audible."""
    session = AsyncMock()
    cached = [{"asin": "B001", "title": "Cached"}]
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=cached)), \
         patch.object(releases, "audible_get", new=AsyncMock()) as mock_get:
        result = await releases.get_new_releases("us", session, days=30)
        assert result == cached
        mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_coming_soon_cache_hit_skips_audible():
    """A cache hit returns immediately without scanning Audible."""
    session = AsyncMock()
    cached = [{"asin": "B002", "title": "Cached"}]
    with patch.object(releases.cache, "get", new=AsyncMock(return_value=cached)), \
         patch.object(releases, "audible_get", new=AsyncMock()) as mock_get:
        result = await releases.get_coming_soon("us", session, days=30)
        assert result == cached
        mock_get.assert_not_called()


# ============================================================
# WINDOWING (single genre)
# ============================================================

@pytest.mark.asyncio
async def test_new_releases_windows_recent_past():
    """New releases collects books within the look-back window and stops below it."""
    # Descending by release date: future (skip), in-window, then below window (stop)
    page = _page(
        _product("BFUTURE", 10),   # future — skipped
        _product("BRECENT1", -5),  # in window
        _product("BRECENT2", -20), # in window
        _product("BOLD", -40),     # below 30-day window — triggers stop
    )
    with patch.object(releases, "get_stored_genres", new=AsyncMock(return_value=_FRESH_ONE)), \
         patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=_audible({"G1": [page]})):
        result = await releases.get_new_releases("us", AsyncMock(), days=30)
        asins = [b["asin"] for b in result]
        assert asins == ["BRECENT1", "BRECENT2"]


@pytest.mark.asyncio
async def test_coming_soon_windows_future_and_sorts_soonest_first():
    """Coming soon collects upcoming books, stops at released, sorts soonest first."""
    page = _page(
        _product("BFAR", 100),     # beyond 30-day window — skipped
        _product("BLATER", 20),    # in window
        _product("BSOONER", 5),    # in window
        _product("BRELEASED", -1), # already released — triggers stop
    )
    with patch.object(releases, "get_stored_genres", new=AsyncMock(return_value=_FRESH_ONE)), \
         patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=_audible({"G1": [page]})):
        result = await releases.get_coming_soon("us", AsyncMock(), days=30)
        asins = [b["asin"] for b in result]
        # soonest first
        assert asins == ["BSOONER", "BLATER"]


@pytest.mark.asyncio
async def test_new_releases_sorted_newest_first():
    """The post-union result is sorted newest-first regardless of page order."""
    page = _page(
        _product("BOLD", -10),
        _product("BNEW", -1),
        _product("BMID", -5),
    )
    with patch.object(releases, "get_stored_genres", new=AsyncMock(return_value=_FRESH_ONE)), \
         patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=_audible({"G1": [page]})):
        result = await releases.get_new_releases("us", AsyncMock(), days=30)
        asins = [b["asin"] for b in result]
        assert asins == ["BNEW", "BMID", "BOLD"]


# ============================================================
# UNION / DEDUPE / WALL (multi-genre walk)
# ============================================================

@pytest.mark.asyncio
async def test_unions_and_dedupes_across_genres():
    """Books from multiple genres union into one list; a shared book appears once."""
    fresh_two = ([{"genre_id": "G1", "name": "G1"}, {"genre_id": "G2", "name": "G2"}], NOW)
    g1 = _page(_product("B1", -2), _product("B2", -3))
    g2 = _page(_product("B2", -3), _product("B3", -4))  # B2 shared with G1
    with patch.object(releases, "get_stored_genres", new=AsyncMock(return_value=fresh_two)), \
         patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=_audible({"G1": [g1], "G2": [g2]})):
        result = await releases.get_new_releases("us", AsyncMock(), days=30)
        asins = [b["asin"] for b in result]
        # newest-first, B2 present exactly once
        assert asins == ["B1", "B2", "B3"]
        assert asins.count("B2") == 1


@pytest.mark.asyncio
async def test_duplicate_page_wall_stops_walk():
    """A genre walk stops when a full page repeats the previous one (the ~535 wall)."""
    # 50 in-window products (== page size) so the short-page check can't pre-empt;
    # the same page returned twice must trigger the duplicate-page wall.
    full = _page(*[_product(f"B{i:02d}", -1) for i in range(50)])
    mock_get = _audible({"G1": [full, full]})
    with patch.object(releases, "get_stored_genres", new=AsyncMock(return_value=_FRESH_ONE)), \
         patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=mock_get):
        result = await releases.get_new_releases("us", AsyncMock(), days=30)
        assert len(result) == 50                      # each book once
        assert mock_get.await_count == 2              # page 0, then the repeat → stop


# ============================================================
# CACHING
# ============================================================

@pytest.mark.asyncio
async def test_new_releases_caches_with_midnight_ttl():
    """On a miss, results get cached with a TTL to the next UTC midnight."""
    page = _page(_product("BRECENT", -2), _product("BOLD", -40))
    with patch.object(releases, "get_stored_genres", new=AsyncMock(return_value=_FRESH_ONE)), \
         patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()) as mock_set, \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=_audible({"G1": [page]})):
        await releases.get_new_releases("us", AsyncMock(), days=30)
        assert mock_set.await_count == 1
        _, kwargs = mock_set.call_args
        assert "ttl_seconds" in kwargs
        assert 0 < kwargs["ttl_seconds"] <= 86400


@pytest.mark.asyncio
async def test_empty_scan_returns_empty_and_does_not_cache():
    """If the walk yields nothing in-window, return empty and don't cache."""
    with patch.object(releases, "get_stored_genres", new=AsyncMock(return_value=_FRESH_ONE)), \
         patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()) as mock_set, \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=_audible({"G1": [_empty()]})):
        result = await releases.get_new_releases("us", AsyncMock(), days=30)
        assert result == []
        mock_set.assert_not_called()


# ============================================================
# GENRE-LIST REFRESH (inline, daily)
# ============================================================

@pytest.mark.asyncio
async def test_genre_list_refreshed_when_stale():
    """A stale stored set triggers a /categories fetch, stores the leaves, walks them."""
    stale = ([{"genre_id": "STALE", "name": "Stale"}], NOW - timedelta(days=2))
    taxonomy = _categories(("P1", "Parent", [("L1", "Leaf One"), ("L2", "Leaf Two")]))
    pages = {"L1": [_page(_product("BA", -2))], "L2": [_page(_product("BB", -3))]}
    mock_upsert = AsyncMock()
    with patch.object(releases, "get_stored_genres", new=AsyncMock(return_value=stale)), \
         patch.object(releases, "upsert_genres", new=mock_upsert), \
         patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=_audible(pages, categories=taxonomy)):
        result = await releases.get_new_releases("us", AsyncMock(), days=30)
        asins = [b["asin"] for b in result]
        assert asins == ["BA", "BB"]
        # the flattened leaves were stored
        assert mock_upsert.await_count == 1
        stored_leaves = mock_upsert.await_args.args[2]
        assert stored_leaves == [
            {"genre_id": "L1", "name": "Leaf One"},
            {"genre_id": "L2", "name": "Leaf Two"},
        ]


@pytest.mark.asyncio
async def test_fetch_catalog_genres_flattens_and_dedupes_leaves():
    """_fetch_catalog_genres keeps only leaves and dedupes ids shared across parents."""
    taxonomy = _categories(
        ("P1", "Par1", [("L1", "a"), ("L2", "b")]),
        ("P2", "Par2", [("L1", "a"), ("L3", "c")]),  # L1 repeated under a second parent
    )
    with patch.object(releases, "audible_get", new=_audible(categories=taxonomy)):
        leaves = await releases._fetch_catalog_genres("us")
        assert [g["genre_id"] for g in leaves] == ["L1", "L2", "L3"]


@pytest.mark.asyncio
async def test_genre_refresh_failure_falls_back_to_stored():
    """If the /categories fetch fails, fall back to the stored genres and walk them."""
    stale = ([{"genre_id": "OLD", "name": "Old"}], NOW - timedelta(days=2))
    pages = {"OLD": [_page(_product("BO", -2))]}
    mock_upsert = AsyncMock()
    with patch.object(releases, "get_stored_genres", new=AsyncMock(return_value=stale)), \
         patch.object(releases, "upsert_genres", new=mock_upsert), \
         patch.object(releases.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(releases.cache, "set", new=AsyncMock()), \
         patch.object(releases, "persist_books_background"), \
         patch.object(releases, "audible_get", new=_audible(pages, categories_error=True)):
        result = await releases.get_new_releases("us", AsyncMock(), days=30)
        asins = [b["asin"] for b in result]
        assert asins == ["BO"]                 # walked the stored genre despite fetch failure
        mock_upsert.assert_not_called()        # nothing stored when the fetch failed