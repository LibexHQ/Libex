"""
Search endpoint tests.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from httpx import AsyncClient, ASGITransport

# Local
from app.main import app

MOCK_BOOK = {
    "asin": "B08G9PRS1K",
    "title": "Test Book",
    "subtitle": None,
    "description": "A test book description",
    "summary": "A test summary",
    "region": "us",
    "regions": ["us"],
    "publisher": "Test Publisher",
    "copyright": None,
    "isbn": None,
    "language": "english",
    "rating": 4.5,
    "bookFormat": None,
    "releaseDate": "2021-01-01",
    "explicit": False,
    "hasPdf": False,
    "whisperSync": False,
    "imageUrl": "https://example.com/cover.jpg",
    "lengthMinutes": 600,
    "link": "https://audible.com/pd/B08G9PRS1K",
    "contentType": "Product",
    "contentDeliveryType": None,
    "episodeNumber": None,
    "episodeType": None,
    "sku": None,
    "skuGroup": None,
    "isListenable": True,
    "isAvailable": True,
    "isBuyable": True,
    "updatedAt": None,
    "authors": [{"id": None, "asin": "B000TEST01", "name": "Test Author", "region": "us", "image": None, "updatedAt": None}],
    "narrators": [{"name": "Test Narrator", "updatedAt": None}],
    "genres": [{"asin": None, "name": "Fiction", "type": "Genres", "betterType": "genre", "updatedAt": None}],
    "series": [],
}


@pytest.fixture
async def async_client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_search_returns_list(async_client):
    """Search endpoint returns a list."""
    with patch("app.api.routes.search.router.search", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/search?title=Dune")
        assert response.status_code == 200
        assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_search_returns_404_on_no_results(async_client):
    """Search endpoint returns 404 when nothing found."""
    with patch("app.api.routes.search.router.search", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/search?title=xyznotarealbook")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_quick_search_requires_keywords(async_client):
    """Quick search endpoint requires keywords parameter."""
    response = await async_client.get("/quick-search")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_quick_search_returns_list(async_client):
    """Quick search endpoint returns a list."""
    with patch("app.api.routes.search.router.quick_search", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/quick-search?keywords=dune")
        assert response.status_code == 200
        assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_quick_search_returns_404_on_no_results(async_client):
    """Quick search endpoint returns 404 when nothing found."""
    with patch("app.api.routes.search.router.quick_search", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/quick-search?keywords=xyznotarealbook")
        assert response.status_code == 404

# ============================================================
# CACHE PARAMETER -- /search stays inert; /quick-search flipped to True
# ============================================================


@pytest.mark.asyncio
async def test_search_cache_false_still_marks_the_response_no_store(async_client):
    """/search is inert -- cache never reaches search() -- but the response
    still gets Cache-Control: no-store on an explicit cache=false, matching
    every other route's contract for that flag even though this one has no
    cache read for it to skip."""
    with patch("app.api.routes.search.router.search", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/search?title=Dune&cache=false")
    assert response.headers["cache-control"] == "no-store"
    # search() takes no cache argument at all -- confirm the call never
    # grew one silently.
    assert "cache" not in mock.call_args.kwargs


@pytest.mark.asyncio
async def test_search_cache_omitted_defaults_to_false_and_still_gets_no_store(async_client):
    """/search's cache param stays False by default (CacheInertParam) --
    unlike the Standard routes, omitting it does not suppress
    Cache-Control the way an explicit cache=true would."""
    with patch("app.api.routes.search.router.search", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/search?title=Dune")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_search_cache_true_sends_no_cache_control_header(async_client):
    """Accepted for compatibility even though it changes nothing about what
    search() does -- apply_cache_control still reads it at face value."""
    with patch("app.api.routes.search.router.search", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/search?title=Dune&cache=true")
    assert "cache-control" not in response.headers


@pytest.mark.asyncio
async def test_quick_search_omits_cache_param_and_reads_the_cache_by_default(async_client):
    with patch("app.api.routes.search.router.quick_search", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/quick-search?keywords=dune")
    assert mock.call_args[0][3] is True


@pytest.mark.asyncio
async def test_quick_search_cache_false_marks_the_response_no_store(async_client):
    with patch("app.api.routes.search.router.quick_search", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/quick-search?keywords=dune&cache=false")
    assert mock.call_args[0][3] is False
    assert response.headers["cache-control"] == "no-store"
