"""
Series endpoint tests.
Tests route structure, parameter validation, and error handling.
Audible API calls are mocked — we test our code not Audible's.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from httpx import AsyncClient, ASGITransport

# Local
from app.main import app
from app.core.exceptions import NotFoundException

MOCK_SERIES = {
    "asin": "B00SERIES1",
    "name": "Dune Chronicles",
    "description": "The Dune Chronicles is a science fiction series.",
    "region": "us",
    "position": None,
    "updatedAt": None,
}

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


# ============================================================
# GET SERIES BY ASIN
# ============================================================

@pytest.mark.asyncio
async def test_get_series_returns_200(async_client):
    """Series endpoint returns 200 with valid ASIN."""
    with patch("app.api.routes.series.router.get_series", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_SERIES
        response = await async_client.get("/series/B00SERIES1")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_series_returns_correct_asin(async_client):
    """Series endpoint returns series with requested ASIN."""
    with patch("app.api.routes.series.router.get_series", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_SERIES
        response = await async_client.get("/series/B00SERIES1")
        assert response.json()["asin"] == "B00SERIES1"


@pytest.mark.asyncio
async def test_get_series_returns_required_fields(async_client):
    """Series endpoint response contains all required fields."""
    with patch("app.api.routes.series.router.get_series", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_SERIES
        response = await async_client.get("/series/B00SERIES1")
        data = response.json()
        for field in ["asin", "region"]:
            assert field in data, f"Missing required field: {field}"


@pytest.mark.asyncio
async def test_get_series_default_region_is_us(async_client):
    """Series endpoint defaults to US region."""
    with patch("app.api.routes.series.router.get_series", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_SERIES
        await async_client.get("/series/B00SERIES1")
        assert mock.call_args[0][1] == "us"


@pytest.mark.asyncio
async def test_get_series_accepts_region_parameter(async_client):
    """Series endpoint passes region parameter to service."""
    with patch("app.api.routes.series.router.get_series", new_callable=AsyncMock) as mock:
        mock.return_value = {**MOCK_SERIES, "region": "uk"}
        await async_client.get("/series/B00SERIES1?region=uk")
        assert mock.call_args[0][1] == "uk"


@pytest.mark.asyncio
async def test_get_series_returns_404_when_not_found(async_client):
    """Series endpoint returns 404 when series not found."""
    with patch("app.api.routes.series.router.get_series", new_callable=AsyncMock) as mock:
        mock.side_effect = NotFoundException("Series not found")
        response = await async_client.get("/series/NOTEXIST01")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_series_description_can_be_none(async_client):
    """Series endpoint handles series with no description."""
    with patch("app.api.routes.series.router.get_series", new_callable=AsyncMock) as mock:
        mock.return_value = {**MOCK_SERIES, "description": None}
        response = await async_client.get("/series/B00SERIES1")
        assert response.status_code == 200
        assert response.json()["description"] is None


@pytest.mark.asyncio
async def test_get_series_name_can_be_none(async_client):
    """Series endpoint handles series with no name."""
    with patch("app.api.routes.series.router.get_series", new_callable=AsyncMock) as mock:
        mock.return_value = {**MOCK_SERIES, "name": None}
        response = await async_client.get("/series/B00SERIES1")
        assert response.status_code == 200
        assert response.json()["name"] is None


# ============================================================
# GET SERIES BOOKS
# ============================================================

@pytest.mark.asyncio
async def test_get_series_books_returns_200(async_client):
    """Series books endpoint returns 200."""
    with patch("app.api.routes.series.router.get_series_books", new_callable=AsyncMock) as mock_series, \
         patch("app.api.routes.series.router.get_books_by_asins", new_callable=AsyncMock) as mock_books:
        mock_series.return_value = ["B08G9PRS1K"]
        mock_books.return_value = [MOCK_BOOK]
        response = await async_client.get("/series/books/B00SERIES1")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_series_books_returns_list_of_books(async_client):
    """Series books endpoint returns a list of full book objects."""
    with patch("app.api.routes.series.router.get_series_books", new_callable=AsyncMock) as mock_series, \
         patch("app.api.routes.series.router.get_books_by_asins", new_callable=AsyncMock) as mock_books:
        mock_series.return_value = ["B08G9PRS1K"]
        mock_books.return_value = [MOCK_BOOK]
        response = await async_client.get("/series/books/B00SERIES1")
        data = response.json()
        assert isinstance(data, list)
        assert data[0]["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_series_books_returns_404_when_not_found(async_client):
    """Series books endpoint returns 404 when series not found."""
    with patch("app.api.routes.series.router.get_series_books", new_callable=AsyncMock) as mock:
        mock.side_effect = NotFoundException("Series not found")
        response = await async_client.get("/series/books/NOTEXIST01")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_series_books_default_region_is_us(async_client):
    """Series books endpoint defaults to US region."""
    with patch("app.api.routes.series.router.get_series_books", new_callable=AsyncMock) as mock_series, \
         patch("app.api.routes.series.router.get_books_by_asins", new_callable=AsyncMock) as mock_books:
        mock_series.return_value = ["B08G9PRS1K"]
        mock_books.return_value = [MOCK_BOOK]
        await async_client.get("/series/books/B00SERIES1")
        assert mock_series.call_args[0][1] == "us"


# ============================================================
# SERIES SEARCH
# ============================================================

@pytest.mark.asyncio
async def test_search_series_returns_200(async_client):
    """Series search endpoint returns 200."""
    with patch("app.api.routes.series.router.search_series", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_SERIES]
        response = await async_client.get("/series/search?name=Dune")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_search_series_requires_name(async_client):
    """Series search endpoint requires name parameter."""
    response = await async_client.get("/series/search")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_search_series_returns_list(async_client):
    """Series search endpoint returns a list."""
    with patch("app.api.routes.series.router.search_series", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_SERIES]
        response = await async_client.get("/series/search?name=Dune")
        assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_search_series_returns_404_when_none_found(async_client):
    """Series search returns 404 when nothing found."""
    with patch("app.api.routes.series.router.search_series", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/series/search?name=NotASeries")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_search_series_all_regions(async_client):
    """Series search works for all supported regions."""
    regions = ["us", "uk", "ca", "au", "de", "fr", "it", "es", "jp", "in", "br"]
    with patch("app.api.routes.series.router.search_series", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_SERIES]
        for region in regions:
            response = await async_client.get(f"/series/search?name=Dune&region={region}")
            assert response.status_code == 200, f"Failed for region: {region}"


@pytest.mark.asyncio
async def test_get_series_rejects_invalid_asin(async_client):
    """Series endpoint rejects malformed ASIN."""
    response = await async_client.get("/series/not-an-asin")
    assert response.status_code == 404
    assert "Invalid ASIN" in response.json()["error"]


@pytest.mark.asyncio
async def test_get_series_books_rejects_invalid_asin(async_client):
    """Series books endpoint rejects malformed ASIN."""
    response = await async_client.get("/series/books/not-an-asin")
    assert response.status_code == 404
    assert "Invalid ASIN" in response.json()["error"]