"""
Books endpoint tests.
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


@pytest.fixture
async def async_client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as c:
        yield c


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
    "releaseDate": "2021-01-01T00:00:00+00:00",
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
    "authors": [{"id": None, "asin": "B000TEST01", "name": "Test Author", "region": "us", "regions": ["us"], "image": None, "updatedAt": None}],
    "narrators": [{"name": "Test Narrator", "updatedAt": None}],
    "genres": [{"asin": None, "name": "Fiction", "type": "Genres", "betterType": "genre", "updatedAt": None}],
    "series": [],
}


@pytest.mark.asyncio
async def test_get_book_returns_200(async_client):
    """Single book endpoint returns 200 with valid ASIN."""
    with patch("app.api.routes.books.router.get_book_by_asin", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_BOOK
        response = await async_client.get("/book/B08G9PRS1K")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_book_returns_correct_asin(async_client):
    """Single book endpoint returns book with requested ASIN."""
    with patch("app.api.routes.books.router.get_book_by_asin", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_BOOK
        response = await async_client.get("/book/B08G9PRS1K")
        data = response.json()
        assert data["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_book_response_has_required_fields(async_client):
    """Single book endpoint response contains all required fields."""
    with patch("app.api.routes.books.router.get_book_by_asin", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_BOOK
        response = await async_client.get("/book/B08G9PRS1K")
        data = response.json()
        required_fields = [
            "asin", "title", "authors", "narrators",
            "series", "region", "imageUrl", "description",
        ]
        for field in required_fields:
            assert field in data, f"Missing required field: {field}"


@pytest.mark.asyncio
async def test_get_book_author_has_regions_field(async_client):
    """Book endpoint author objects include regions list matching AudiMeta."""
    with patch("app.api.routes.books.router.get_book_by_asin", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_BOOK
        response = await async_client.get("/book/B08G9PRS1K")
        data = response.json()
        assert "regions" in data["authors"][0]
        assert isinstance(data["authors"][0]["regions"], list)


@pytest.mark.asyncio
async def test_get_book_release_date_is_iso(async_client):
    """Book endpoint releaseDate is in ISO 8601 format."""
    with patch("app.api.routes.books.router.get_book_by_asin", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_BOOK
        response = await async_client.get("/book/B08G9PRS1K")
        data = response.json()
        if data["releaseDate"]:
            assert "T" in data["releaseDate"]


@pytest.mark.asyncio
async def test_get_book_default_region_is_us(async_client):
    """Book endpoint defaults to US region when not specified."""
    with patch("app.api.routes.books.router.get_book_by_asin", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_BOOK
        await async_client.get("/book/B08G9PRS1K")
        call_args = mock.call_args
        assert call_args[0][1] == "us"


@pytest.mark.asyncio
async def test_get_book_accepts_region_parameter(async_client):
    """Book endpoint passes region parameter to service."""
    with patch("app.api.routes.books.router.get_book_by_asin", new_callable=AsyncMock) as mock:
        mock.return_value = {**MOCK_BOOK, "region": "uk"}
        await async_client.get("/book/B08G9PRS1K?region=uk")
        call_args = mock.call_args
        assert call_args[0][1] == "uk"


@pytest.mark.asyncio
async def test_bulk_books_requires_asins(async_client):
    """Bulk book endpoint requires asins parameter."""
    response = await async_client.get("/book")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_bulk_books_returns_correct_structure(async_client):
    """Bulk book endpoint returns books and notFound dict."""
    with patch("app.api.routes.books.router.get_books_by_asins", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/book?asins=B08G9PRS1K")
        assert response.status_code == 200
        data = response.json()
        assert "books" in data
        assert "notFound" in data
        assert isinstance(data["books"], list)
        assert isinstance(data["notFound"], list)


@pytest.mark.asyncio
async def test_bulk_books_not_found_asins_listed(async_client):
    """Bulk book endpoint lists ASINs not found."""
    with patch("app.api.routes.books.router.get_books_by_asins", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/book?asins=B08G9PRS1K,B000000001")
        data = response.json()
        assert "B000000001" in data["notFound"]


@pytest.mark.asyncio
async def test_bulk_books_rejects_over_1000_asins(async_client):
    """Bulk book endpoint rejects requests with more than 1000 ASINs."""
    with patch("app.api.routes.books.router.get_books_by_asins", new_callable=AsyncMock) as mock:
        mock.return_value = []
        asins = ",".join([f"B{str(i).zfill(9)}" for i in range(1001)])
        response = await async_client.get(f"/book?asins={asins}")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_book_rejects_invalid_asin(async_client):
    """Book endpoint rejects malformed ASIN."""
    response = await async_client.get("/book/not-an-asin")
    assert response.status_code == 404
    assert "Invalid ASIN" in response.json()["error"]


@pytest.mark.asyncio
async def test_get_book_chapters_rejects_invalid_asin(async_client):
    """Chapters endpoint rejects malformed ASIN."""
    response = await async_client.get("/book/not-an-asin/chapters")
    assert response.status_code == 404
    assert "Invalid ASIN" in response.json()["error"]


@pytest.mark.asyncio
async def test_bulk_books_rejects_invalid_asin_in_list(async_client):
    """Bulk book endpoint rejects list containing invalid ASIN."""
    response = await async_client.get("/book?asins=B08G9PRS1K,not-an-asin")
    assert response.status_code == 404
    assert "Invalid ASIN" in response.json()["error"]