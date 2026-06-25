"""
Releases route tests.

Covers the /categories discovery endpoint and the category param plumbing on the
live /new-releases and /coming-soon endpoints. Services are mocked at the
router's import location.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest


# Flat node list as _ensure_genres returns it: parents have parent_id="",
# leaves carry their parent's id.
_NODES = [
    {"genre_id": "P1", "parent_id": "", "name": "History"},
    {"genre_id": "P2", "parent_id": "", "name": "Sci-Fi"},
    {"genre_id": "L1", "parent_id": "P1", "name": "Ancient"},
    {"genre_id": "L2", "parent_id": "P1", "name": "Modern"},
    {"genre_id": "L3", "parent_id": "P2", "name": "Space"},
]


# ============================================================
# /categories
# ============================================================

@pytest.mark.asyncio
async def test_categories_returns_nested_tree(async_client):
    """/categories nests leaves under their parents, sorted by name."""
    with patch("app.api.routes.releases.router._ensure_genres", new_callable=AsyncMock) as mock:
        mock.return_value = _NODES
        response = await async_client.get("/categories?region=us")
        assert response.status_code == 200
        data = response.json()
    # two parents, alphabetical
    assert [p["name"] for p in data] == ["History", "Sci-Fi"]
    history = next(p for p in data if p["id"] == "P1")
    assert [c["name"] for c in history["children"]] == ["Ancient", "Modern"]
    scifi = next(p for p in data if p["id"] == "P2")
    assert [c["id"] for c in scifi["children"]] == ["L3"]


@pytest.mark.asyncio
async def test_categories_nests_deep_tree(async_client):
    """
    /categories nests every level, not just two. A flat node list spanning four
    levels (parent -> child -> grandchild -> great-grandchild) comes back as a
    fully nested tree, each node carrying its own children, and a childless node
    ends its branch with an empty children list.
    """
    nodes = [
        {"genre_id": "P1", "parent_id": "", "name": "Arts"},
        {"genre_id": "C1", "parent_id": "P1", "name": "Performing"},
        {"genre_id": "C2", "parent_id": "P1", "name": "Architecture"},  # childless leaf
        {"genre_id": "G1", "parent_id": "C1", "name": "Film & TV"},
        {"genre_id": "GG1", "parent_id": "G1", "name": "Direction"},    # depth 4
    ]
    with patch("app.api.routes.releases.router._ensure_genres", new_callable=AsyncMock) as mock:
        mock.return_value = nodes
        response = await async_client.get("/categories?region=us")
        assert response.status_code == 200
        data = response.json()

    arts = next(p for p in data if p["id"] == "P1")
    # children of Arts, alphabetical: Architecture (childless), Performing
    assert [c["name"] for c in arts["children"]] == ["Architecture", "Performing"]
    architecture = next(c for c in arts["children"] if c["id"] == "C2")
    assert architecture["children"] == []
    performing = next(c for c in arts["children"] if c["id"] == "C1")
    film = next(g for g in performing["children"] if g["id"] == "G1")
    # the great-grandchild is nested under the grandchild — depth 4 round-trips
    assert [gg["id"] for gg in film["children"]] == ["GG1"]


@pytest.mark.asyncio
async def test_categories_dual_parent_leaf_appears_under_both(async_client):
    """A leaf under two parents shows up under each."""
    nodes = [
        {"genre_id": "P1", "parent_id": "", "name": "History"},
        {"genre_id": "P2", "parent_id": "", "name": "Society"},
        {"genre_id": "LX", "parent_id": "P1", "name": "Shared"},
        {"genre_id": "LX", "parent_id": "P2", "name": "Shared"},
    ]
    with patch("app.api.routes.releases.router._ensure_genres", new_callable=AsyncMock) as mock:
        mock.return_value = nodes
        response = await async_client.get("/categories?region=us")
        assert response.status_code == 200
        data = response.json()
    for parent in data:
        assert [c["id"] for c in parent["children"]] == ["LX"]


@pytest.mark.asyncio
async def test_categories_empty_returns_404(async_client):
    """No taxonomy available -> 404 with the LibexException error body."""
    with patch("app.api.routes.releases.router._ensure_genres", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/categories?region=us")
        assert response.status_code == 404
        assert "categories" in response.json()["error"].lower()


@pytest.mark.asyncio
async def test_categories_invalid_region_returns_400(async_client):
    """An invalid region is rejected by the valid_region dependency."""
    response = await async_client.get("/categories?region=zz")
    assert response.status_code == 400


# ============================================================
# category param plumbing on the live endpoints
# ============================================================

@pytest.mark.asyncio
async def test_new_releases_passes_category_to_service(async_client):
    """The category query param is forwarded to get_new_releases."""
    with patch("app.api.routes.releases.router.get_new_releases", new_callable=AsyncMock) as mock:
        mock.return_value = []
        await async_client.get("/new-releases?region=us&category=C1")
        # signature: get_new_releases(region, session, days, category)
        assert mock.await_args.args[3] == "C1"


@pytest.mark.asyncio
async def test_new_releases_without_category_passes_none(async_client):
    """Omitting category forwards None (the un-categoried sample path)."""
    with patch("app.api.routes.releases.router.get_new_releases", new_callable=AsyncMock) as mock:
        mock.return_value = []
        await async_client.get("/new-releases?region=us")
        assert mock.await_args.args[3] is None


@pytest.mark.asyncio
async def test_coming_soon_passes_category_to_service(async_client):
    """The category query param is forwarded to get_coming_soon."""
    with patch("app.api.routes.releases.router.get_coming_soon", new_callable=AsyncMock) as mock:
        mock.return_value = []
        await async_client.get("/coming-soon?region=us&category=C2")
        assert mock.await_args.args[3] == "C2"