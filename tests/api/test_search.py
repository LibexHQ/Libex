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
    "authors": [{"name": "Test Author", "asin": "B000TEST01", "region": "us"}],
    "narrators": ["Test Narrator"],
    "series": [],
    "series_name": None,
    "series_asin": None,
    "series_position": None,
    "series_region": None,
    "cover_url": "https://example.com/cover.jpg",
    "description": "A test book description",
    "summary": "A test summary",
    "publisher": "Test Publisher",
    "language": "english",
    "runtime_length_min": 600,
    "rating": 4.5,
    "genres": ["Fiction"],
    "release_date": "2021-01-01",
    "explicit": False,
    "has_pdf": False,
    "whisper_sync": False,
    "isbn": None,
    "content_type": "Product",
    "sku": None,
    "region": "us",
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
async def test_search_returns_empty_list_on_no_results(async_client):
    """Search endpoint returns empty list when nothing found."""
    with patch("app.api.routes.search.router.search", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/search?title=xyznotarealbook")
        assert response.status_code == 200
        assert response.json() == []


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