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
            "series", "region", "cover_url", "description",
        ]
        for field in required_fields:
            assert field in data, f"Missing required field: {field}"


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
async def test_bulk_books_returns_list(async_client):
    """Bulk book endpoint returns a list."""
    with patch("app.api.routes.books.router.get_books_by_asins", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/book?asins=B08G9PRS1K")
        assert response.status_code == 200
        assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_bulk_books_rejects_over_1000_asins(async_client):
    """Bulk book endpoint rejects requests with more than 1000 ASINs."""
    with patch("app.api.routes.books.router.get_books_by_asins", new_callable=AsyncMock) as mock:
        mock.return_value = []
        asins = ",".join([f"B{str(i).zfill(9)}" for i in range(1001)])
        response = await async_client.get(f"/book?asins={asins}")
        assert response.status_code == 404