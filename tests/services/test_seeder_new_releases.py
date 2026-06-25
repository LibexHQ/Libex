"""
Service tests for the seeder's new-releases catalog walk (_scan_new_releases).

The seeder reconstructs new-releases coverage by walking every catalog node —
parents AND leaves — by -ReleaseDate, collecting ALL reachable ASINs (future
pre-orders and recent releases alike, no date gate) and persisting the ones not
already in the DB. It is fully self-contained: it fetches the taxonomy itself
and never touches the catalog_genres table or the live release path.

Resilience: each node is walked independently, so one failed Audible call skips
that node and the scan continues — it does not abort the whole cycle.

These tests mock the deferred Audible client (audible_get is imported inside the
seeder's functions, so it's patched at its source, app.services.audible.client),
the DB-missing check, and the persist boundary, so we exercise the collect-all
walk, the duplicate-page wall, parent+leaf coverage, cross-node dedupe, the junk
filter, the persist-only-missing behavior, and per-node resilience — without
real HTTP or DB.
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
    return {"asin": asin, "title": f"Book {asin}" if title is None else title}


def _page(*products):
    return {"products": list(products)}


def _catnode(spec):
    """Builds one taxonomy node from (id, name) or (id, name, [child_specs]).
    Recurses, so children can carry their own children to any depth."""
    if len(spec) == 2:
        nid, name = spec
        kids = []
    else:
        nid, name, kids = spec
    return {"id": nid, "name": name, "children": [_catnode(k) for k in kids]}


def _categories(*parents):
    """Taxonomy: each parent is (id, name) or (id, name, [child_specs]); child
    specs nest to any depth, so this builds the full multi-level tree."""
    return {"categories": [_catnode(p) for p in parents]}


def _audible(pages_by_node=None, categories=None, error_on=None):
    """
    Async audible_get stand-in: serves the taxonomy on a /categories call and
    product pages keyed by category_id (empty once exhausted). error_on can be
    'categories' or a specific node id, raising on that call.
    """
    pages_by_node = pages_by_node or {}

    def _get(region, path, params=None):
        params = params or {}
        if "/categories" in path:
            if error_on == "categories":
                raise RuntimeError("categories unavailable")
            return categories if categories is not None else {"categories": []}
        nid = params.get("category_id")
        if error_on == nid:
            raise RuntimeError(f"node {nid} unavailable")
        page = params.get("page", 0)
        these = pages_by_node.get(nid, [])
        return these[page] if page < len(these) else {"products": []}

    return AsyncMock(side_effect=_get)


# A taxonomy with a single parent and single leaf. _fetch_catalog_genres emits
# BOTH nodes (parent "P1" + leaf "G1"), so the walk visits two category ids.
def _parent_and_leaf(parent="P1", leaf="G1"):
    return _categories((parent, "Parent", [(leaf, "Leaf")]))


# ============================================================
# COLLECT-ALL (no date gate) + PARENT/LEAF
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

    # Parent P1 serves the page; leaf G1 serves nothing — union is the same set.
    with patch(_AUDIBLE_GET, new=_audible({"P1": [page]}, categories=_parent_and_leaf())), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(side_effect=_missing)), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock()):
        await seeder._scan_new_releases("us", delay=0)
        assert set(captured["asins"]) == {"BFUTURE", "BRECENT", "BOLD"}


@pytest.mark.asyncio
async def test_walks_parent_and_leaf_and_unions():
    """Parent-only titles AND leaf-only titles are both collected (parent isn't a superset)."""
    taxonomy = _parent_and_leaf("P1", "G1")
    parent_page = _page(_product("BPARENT", -1), _product("BSHARED", -2))
    leaf_page = _page(_product("BSHARED", -2), _product("BLEAF", -3))  # BSHARED also under leaf
    captured = {}

    async def _missing(session, asins):
        captured["asins"] = list(asins)
        return list(asins)

    with patch(_AUDIBLE_GET, new=_audible({"P1": [parent_page], "G1": [leaf_page]}, categories=taxonomy)), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(side_effect=_missing)), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock()):
        await seeder._scan_new_releases("us", delay=0)
        assert sorted(captured["asins"]) == ["BLEAF", "BPARENT", "BSHARED"]
        assert captured["asins"].count("BSHARED") == 1  # deduped across nodes


@pytest.mark.asyncio
async def test_walks_deep_taxonomy_nodes():
    """
    The taxonomy runs up to five levels deep, and each level surfaces titles the
    level above misses, so the scan must walk every node at every level — not just
    parents and leaves. A grandchild and a great-grandchild node are each served
    their own page; the scan visits both and unions their ASINs in.
    """
    taxonomy = _categories(
        ("P1", "Parent", [
            ("C1", "Child", [
                ("G1", "Grandchild", [
                    ("GG1", "GreatGrand"),
                ]),
            ]),
        ]),
    )
    pages = {
        "P1": [_page(_product("BP", -1))],
        "C1": [_page(_product("BC", -2))],
        "G1": [_page(_product("BG", -3))],
        "GG1": [_page(_product("BGG", -4))],
    }
    captured = {}

    async def _missing(session, asins):
        captured["asins"] = list(asins)
        return list(asins)

    with patch(_AUDIBLE_GET, new=_audible(pages, categories=taxonomy)), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(side_effect=_missing)), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock()):
        await seeder._scan_new_releases("us", delay=0)
        # every level was walked, including the great-grandchild (depth 4)
        assert sorted(captured["asins"]) == ["BC", "BG", "BGG", "BP"]


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

    with patch(_AUDIBLE_GET, new=_audible({"P1": [page]}, categories=_parent_and_leaf())), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(side_effect=_missing)), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock()):
        await seeder._scan_new_releases("us", delay=0)
        assert captured["asins"] == ["BGOOD"]


# ============================================================
# PAGING / WALL  (counts account for parent + leaf = 2 nodes)
# ============================================================

@pytest.mark.asyncio
async def test_duplicate_page_wall_stops_node_walk():
    """A node walk stops when a full page repeats the previous one (the ~535 wall)."""
    full = _page(*[_product(f"B{i:02d}", -1) for i in range(50)])
    # Both parent and leaf return the same repeated page -> each stops after 2 pages.
    mock_get = _audible({"P1": [full, full], "G1": [full, full]}, categories=_parent_and_leaf())
    captured = {}

    async def _missing(session, asins):
        captured["asins"] = list(asins)
        return []

    with patch(_AUDIBLE_GET, new=mock_get), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(side_effect=_missing)), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock()):
        await seeder._scan_new_releases("us", delay=0)
        assert len(captured["asins"]) == 50  # deduped across both nodes
        product_calls = [c for c in mock_get.await_args_list if "/products" in c.args[1]]
        # 2 nodes x (page 0 + repeat -> stop) = 4 product calls
        assert len(product_calls) == 4


@pytest.mark.asyncio
async def test_short_page_stops_node_walk():
    """A sub-page-size page ends a node walk — no second page for that node."""
    short = _page(_product("B1", -1), _product("B2", -2))   # 2 < 50
    mock_get = _audible({"P1": [short], "G1": [short]}, categories=_parent_and_leaf())

    with patch(_AUDIBLE_GET, new=mock_get), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(return_value=[])), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock()):
        await seeder._scan_new_releases("us", delay=0)
        product_calls = [c for c in mock_get.await_args_list if "/products" in c.args[1]]
        # 2 nodes x 1 page each = 2 product calls
        assert len(product_calls) == 2


# ============================================================
# PERSISTENCE BOUNDARY
# ============================================================

@pytest.mark.asyncio
async def test_only_missing_asins_persisted():
    """Only ASINs not already in the DB are handed to _fetch_and_persist."""
    page = _page(_product("BHAVE", -1), _product("BNEW1", -2), _product("BNEW2", -3))
    mock_persist = AsyncMock()

    with patch(_AUDIBLE_GET, new=_audible({"P1": [page]}, categories=_parent_and_leaf())), \
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

    with patch(_AUDIBLE_GET, new=_audible({"P1": [page]}, categories=_parent_and_leaf())), \
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
async def test_taxonomy_failure_aborts_with_error():
    """A failed taxonomy fetch returns stats with an error and persists nothing."""
    with patch(_AUDIBLE_GET, new=_audible(error_on="categories")), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(return_value=[])), \
         patch.object(seeder, "_fetch_and_persist", new=AsyncMock()):
        stats = await seeder._scan_new_releases("us", delay=0)
        assert stats["errors"] == 1
        assert stats["books_discovered"] == 0


@pytest.mark.asyncio
async def test_one_failed_node_does_not_abort_scan():
    """A failing node is skipped (errors += 1) while the rest of the scan proceeds."""
    taxonomy = _parent_and_leaf("P1", "G1")
    leaf_page = _page(_product("BLEAF", -1))
    # Parent P1 raises; leaf G1 succeeds -> we still collect BLEAF and persist it.
    mock_get = _audible({"G1": [leaf_page]}, categories=taxonomy, error_on="P1")
    mock_persist = AsyncMock()
    captured = {}

    async def _missing(session, asins):
        captured["asins"] = list(asins)
        return list(asins)

    with patch(_AUDIBLE_GET, new=mock_get), \
         patch.object(seeder, "SessionFactory"), \
         patch.object(seeder, "_get_missing_asins", new=AsyncMock(side_effect=_missing)), \
         patch.object(seeder, "_fetch_and_persist", new=mock_persist):
        stats = await seeder._scan_new_releases("us", delay=0)
        assert stats["errors"] == 1           # the failed parent node
        assert captured["asins"] == ["BLEAF"]  # leaf still collected
        assert mock_persist.await_count == 1   # and persisted
        assert stats["books_discovered"] == 1

# ============================================================
# _get_missing_asins — chunked IN query (param-cap regression)
# ============================================================

@pytest.mark.asyncio
async def test_get_missing_asins_chunks_large_lists():
    """The IN query is chunked at 5000 so it never exceeds Postgres's 32767
    bind-parameter cap — the genre-union scan can pass tens of thousands of ASINs.
    Regression for the 'number of query arguments cannot exceed 32767' crash."""
    from unittest.mock import MagicMock

    # 12,001 ASINs -> ceil(12001 / 5000) = 3 chunks -> 3 execute calls.
    asins = [f"B{i:09d}" for i in range(12001)]

    def _execute(_stmt):
        result = MagicMock()
        result.fetchall.return_value = []
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)

    missing = await seeder._get_missing_asins(session, asins)

    assert session.execute.await_count == 3          # chunked, not one giant query
    assert len(missing) == 12001                      # all missing (nothing existed)


@pytest.mark.asyncio
async def test_get_missing_asins_filters_existing():
    """Existing ASINs (returned by the DB) are removed; the existing set is
    accumulated across chunks."""
    from unittest.mock import MagicMock

    asins = [f"B{i:09d}" for i in range(7000)]  # 2 chunks (5000 + 2000)
    already = {"B000000003", "B000006500"}

    def _execute(stmt):
        result = MagicMock()
        if not hasattr(_execute, "_called"):
            _execute._called = True
            result.fetchall.return_value = [(a,) for a in already]
        else:
            result.fetchall.return_value = []
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)

    missing = await seeder._get_missing_asins(session, asins)

    assert session.execute.await_count == 2          # 7000 -> 2 chunks
    assert "B000000003" not in missing
    assert "B000006500" not in missing
    assert len(missing) == 7000 - 2


@pytest.mark.asyncio
async def test_get_missing_asins_empty_short_circuits():
    """An empty input returns [] without touching the DB."""
    session = AsyncMock()
    session.execute = AsyncMock()
    result = await seeder._get_missing_asins(session, [])
    assert result == []
    session.execute.assert_not_awaited()
