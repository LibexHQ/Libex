"""
Service tests for the seeder's new-releases leaf-walk (_scan_new_releases).

The seeder reconstructs new-releases coverage by walking every LEAF genre's
catalog (category_id + -ReleaseDate) and collecting ALL reachable ASINs — future
pre-orders and recent releases alike, with no date gate — then persisting the
ones not already in the DB. It is fully self-contained: it fetches the taxonomy
itself and never touches the catalog_genres table or the live release path.

These tests mock the deferred Audible client (audible_get is imported inside the
seeder's functions, so it's patched at its source, app.services.audible.client),
the DB-missing check, and the persist boundary, so we exercise the collect-all
walk, the duplicate-page wall, cross-genre dedupe, the junk filter, and the
persist-only-missing behavior — without real HTTP or DB.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Services
from app.services import seeder

_AUDIBLE_GET = "app.services.audible.client.audible_get"


def _product(asin, days_offset, title=None):
    """Minimal raw catalog product. days_offset is unused by the collect-all walk
    (no date gate) but kept so tests can mix future/past explicitly."""
    p = {"asin": asin, "title": f"Book {asin}" if title is None else title}
    return p


def _page(*products):
    return {"products": list(products)}


def _categories(*parents):
    """Taxonomy: each parent is (parent_id, parent_name, [(leaf_id, leaf_name), ...])."""
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


def _audible(pages_by_genre=None, categories=None, error_on=None):
    """
    Async audible_get stand-in: serves the taxonomy on a /categories call and
    per-genre product pages keyed by category_id (empty once exhausted).
    error_on='categories' or a genre id raises on that call.
    """
    pages_by_genre = pages_by_genre or {}

    def _get(region, path, params=None):
        params = params or {}
        if "/categories" in path:
            if error_on == "categories":
                raise RuntimeError("categories unavailable")
            return categories if categories is not None else {"categories": []}
        gid = params.get("category_id")
        if error_on == gid:
            raise RuntimeError(f"genre {gid} unavailable")
        page = params.get("page", 0)
        pages = pages_by_genre.get(gid, [])
        return pages[page] if page < len(pages) else {"products": []}

    return AsyncMock(side_effect=_get)


def _one_genre(gid="G1", name="G1"):
    return _categories(("P1", "Parent", [(gid, name)]))


# ============================================================
# COLLECT-ALL (no date gate)
# ============================================================

@pytest.mark.asyncio
async def test_collect_all_grabs_every_asin_no_date_gate():
    """The walk collects every ASIN — future pre-orders and old releases alike."""
    page = _page(
        _product("BFUTURE", 120),   # far-future pre-order — still collected
        _product("BRECENT", -2),
        _product("BOLD", -400),     # ancient — still collected (no window)
    )
    captured = {}

    async def _missing(session, asins):
        captured["asins"] = list(asins)
        return list(asins)

    with patch(_AUDIBLE_GET, new=_audible({"G1": [page]}, categories=_one_genre())), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(side_effect=_missing)), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock()):
        await seeder._scan_new_releases("us", delay=0)
        assert set(captured["asins"]) == {"BFUTURE", "BRECENT", "BOLD"}


@pytest.mark.asyncio
async def test_unions_and_dedupes_across_genres():
    """A book appearing under two genres is collected exactly once."""
    taxonomy = _categories(("P1", "Parent", [("G1", "One"), ("G2", "Two")]))
    g1 = _page(_product("B1", -1), _product("B2", -2))
    g2 = _page(_product("B2", -2), _product("B3", -3))  # B2 shared
    captured = {}

    async def _missing(session, asins):
        captured["asins"] = list(asins)
        return list(asins)

    with patch(_AUDIBLE_GET, new=_audible({"G1": [g1], "G2": [g2]}, categories=taxonomy)), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(side_effect=_missing)), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock()):
        await seeder._scan_new_releases("us", delay=0)
        assert sorted(captured["asins"]) == ["B1", "B2", "B3"]
        assert captured["asins"].count("B2") == 1


@pytest.mark.asyncio
async def test_titleless_products_skipped():
    """Products with no title are dropped from the collected set."""
    page = _page(
        _product("BGOOD", -1),
        _product("BJUNK", -1, title=""),   # no title — skipped
    )
    captured = {}

    async def _missing(session, asins):
        captured["asins"] = list(asins)
        return list(asins)

    with patch(_AUDIBLE_GET, new=_audible({"G1": [page]}, categories=_one_genre())), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(side_effect=_missing)), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock()):
        await seeder._scan_new_releases("us", delay=0)
        assert captured["asins"] == ["BGOOD"]


# ============================================================
# PAGING / WALL
# ============================================================

@pytest.mark.asyncio
async def test_duplicate_page_wall_stops_genre_walk():
    """A genre walk stops when a full page repeats the previous one (the ~535 wall)."""
    full = _page(*[_product(f"B{i:02d}", -1) for i in range(50)])
    mock_get = _audible({"G1": [full, full]}, categories=_one_genre())
    captured = {}

    async def _missing(session, asins):
        captured["asins"] = list(asins)
        return []

    with patch(_AUDIBLE_GET, new=mock_get), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(side_effect=_missing)), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock()):
        await seeder._scan_new_releases("us", delay=0)
        assert len(captured["asins"]) == 50          # each book once
        # 1 taxonomy call + 2 product pages (page 0, then the repeat → stop)
        product_calls = [
            c for c in mock_get.await_args_list
            if "/products" in c.args[1]
        ]
        assert len(product_calls) == 2


@pytest.mark.asyncio
async def test_short_page_stops_genre_walk():
    """A sub-page-size page ends the genre walk — no second page is requested."""
    short = _page(_product("B1", -1), _product("B2", -2))   # 2 < 50
    mock_get = _audible({"G1": [short]}, categories=_one_genre())

    with patch(_AUDIBLE_GET, new=mock_get), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(return_value=[])), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock()):
        await seeder._scan_new_releases("us", delay=0)
        product_calls = [
            c for c in mock_get.await_args_list
            if "/products" in c.args[1]
        ]
        assert len(product_calls) == 1               # only page 0 fetched


# ============================================================
# PERSISTENCE BOUNDARY
# ============================================================

@pytest.mark.asyncio
async def test_only_missing_asins_persisted():
    """Only ASINs not already in the DB are handed to _fetch_and_persist."""
    page = _page(_product("BHAVE", -1), _product("BNEW1", -2), _product("BNEW2", -3))
    mock_persist = AsyncMock()

    with patch(_AUDIBLE_GET, new=_audible({"G1": [page]}, categories=_one_genre())), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(return_value=["BNEW1", "BNEW2"])), \
         patch.object(seeder, "_fetch_and_persist", new=mock_persist):
        stats = await seeder._scan_new_releases("us", delay=0)
        assert mock_persist.await_count == 1
        assert mock_persist.await_args.args[0] == ["BNEW1", "BNEW2"]
        assert stats["books_discovered"] == 2


@pytest.mark.asyncio
async def test_no_missing_skips_persist():
    """When nothing is missing, persist is not called and nothing is counted new."""
    page = _page(_product("BHAVE1", -1), _product("BHAVE2", -2))
    mock_persist = AsyncMock()

    with patch(_AUDIBLE_GET, new=_audible({"G1": [page]}, categories=_one_genre())), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(return_value=[])), \
         patch.object(seeder, "_fetch_and_persist", new=mock_persist):
        stats = await seeder._scan_new_releases("us", delay=0)
        mock_persist.assert_not_called()
        assert stats["books_discovered"] == 0


# ============================================================
# RESILIENCE
# ============================================================

@pytest.mark.asyncio
async def test_scan_handles_audible_failure():
    """A failed Audible call is swallowed: returns stats with errors, never throws."""
    with patch(_AUDIBLE_GET, new=_audible(error_on="categories")), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(return_value=[])), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock()):
        stats = await seeder._scan_new_releases("us", delay=0)
        assert stats["errors"] == 1
        assert stats["books_discovered"] == 0