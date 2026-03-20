"""
Authors endpoint tests.
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

MOCK_AUTHOR = {
    "asin": "B000APF21M",
    "name": "Frank Herbert",
    "description": "Frank Herbert was an American science fiction author.",
    "image": "https://example.com/frank-herbert.jpg",
    "region": "us",
}

MOCK_AUTHOR_BOOKS = ["B08G9PRS1K", "B000000001", "B000000002"]


@pytest.fixture
async def async_client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as c:
        yield c


# ============================================================
# GET AUTHOR BY ASIN
# ============================================================

@pytest.mark.asyncio
async def test_get_author_returns_200(async_client):
    """Author endpoint returns 200 with valid ASIN."""
    with patch("app.api.routes.authors.router.get_author", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_AUTHOR
        response = await async_client.get("/author/B000APF21M")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_author_returns_correct_asin(async_client):
    """Author endpoint returns author with requested ASIN."""
    with patch("app.api.routes.authors.router.get_author", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_AUTHOR
        response = await async_client.get("/author/B000APF21M")
        assert response.json()["asin"] == "B000APF21M"


@pytest.mark.asyncio
async def test_get_author_returns_required_fields(async_client):
    """Author endpoint response contains all required fields."""
    with patch("app.api.routes.authors.router.get_author", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_AUTHOR
        response = await async_client.get("/author/B000APF21M")
        data = response.json()
        for field in ["asin", "name", "region"]:
            assert field in data, f"Missing required field: {field}"


@pytest.mark.asyncio
async def test_get_author_default_region_is_us(async_client):
    """Author endpoint defaults to US region."""
    with patch("app.api.routes.authors.router.get_author", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_AUTHOR
        await async_client.get("/author/B000APF21M")
        assert mock.call_args[0][1] == "us"


@pytest.mark.asyncio
async def test_get_author_accepts_region_parameter(async_client):
    """Author endpoint passes region parameter to service."""
    with patch("app.api.routes.authors.router.get_author", new_callable=AsyncMock) as mock:
        mock.return_value = {**MOCK_AUTHOR, "region": "uk"}
        await async_client.get("/author/B000APF21M?region=uk")
        assert mock.call_args[0][1] == "uk"


@pytest.mark.asyncio
async def test_get_author_returns_404_when_not_found(async_client):
    """Author endpoint returns 404 when author not found."""
    with patch("app.api.routes.authors.router.get_author", new_callable=AsyncMock) as mock:
        mock.side_effect = NotFoundException("Author not found")
        response = await async_client.get("/author/NOTEXIST01")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_author_description_can_be_none(async_client):
    """Author endpoint handles authors with no description."""
    with patch("app.api.routes.authors.router.get_author", new_callable=AsyncMock) as mock:
        mock.return_value = {**MOCK_AUTHOR, "description": None}
        response = await async_client.get("/author/B000APF21M")
        assert response.status_code == 200
        assert response.json()["description"] is None


@pytest.mark.asyncio
async def test_get_author_image_can_be_none(async_client):
    """Author endpoint handles authors with no image."""
    with patch("app.api.routes.authors.router.get_author", new_callable=AsyncMock) as mock:
        mock.return_value = {**MOCK_AUTHOR, "image": None}
        response = await async_client.get("/author/B000APF21M")
        assert response.status_code == 200
        assert response.json()["image"] is None


# ============================================================
# GET AUTHOR BOOKS BY ASIN
# ============================================================

@pytest.mark.asyncio
async def test_get_author_books_returns_200(async_client):
    """Author books endpoint returns 200 with valid ASIN."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_AUTHOR_BOOKS
        response = await async_client.get("/author/books/B000APF21M")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_author_books_returns_asin_list(async_client):
    """Author books endpoint returns list of ASINs."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_AUTHOR_BOOKS
        response = await async_client.get("/author/books/B000APF21M")
        data = response.json()
        assert "book_asins" in data
        assert isinstance(data["book_asins"], list)


@pytest.mark.asyncio
async def test_get_author_books_returns_correct_total(async_client):
    """Author books endpoint returns correct total count."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_AUTHOR_BOOKS
        response = await async_client.get("/author/books/B000APF21M")
        data = response.json()
        assert data["total"] == len(MOCK_AUTHOR_BOOKS)


@pytest.mark.asyncio
async def test_get_author_books_returns_404_when_not_found(async_client):
    """Author books endpoint returns 404 when author not found."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock:
        mock.side_effect = NotFoundException("Author not found")
        response = await async_client.get("/author/books/NOTEXIST01")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_author_books_default_region_is_us(async_client):
    """Author books endpoint defaults to US region."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_AUTHOR_BOOKS
        await async_client.get("/author/books/B000APF21M")
        assert mock.call_args[0][1] == "us"


@pytest.mark.asyncio
async def test_get_author_books_returns_empty_list(async_client):
    """Author books endpoint handles empty book list gracefully."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/author/books/B000APF21M")
        assert response.status_code == 200
        assert response.json()["total"] == 0


# ============================================================
# GET AUTHOR BOOKS BY NAME
# ============================================================

@pytest.mark.asyncio
async def test_get_author_books_by_name_returns_200(async_client):
    """Author books by name endpoint returns 200."""
    with patch("app.api.routes.authors.router.get_author_books_by_name", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_AUTHOR_BOOKS
        response = await async_client.get("/author/books?name=Frank+Herbert")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_author_books_by_name_requires_name(async_client):
    """Author books by name endpoint requires name parameter."""
    response = await async_client.get("/author/books")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_author_books_by_name_returns_asin_list(async_client):
    """Author books by name endpoint returns list of ASINs."""
    with patch("app.api.routes.authors.router.get_author_books_by_name", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_AUTHOR_BOOKS
        response = await async_client.get("/author/books?name=Frank+Herbert")
        data = response.json()
        assert "book_asins" in data
        assert isinstance(data["book_asins"], list)


@pytest.mark.asyncio
async def test_get_author_books_by_name_returns_404_when_not_found(async_client):
    """Author books by name endpoint returns 404 when no books found."""
    with patch("app.api.routes.authors.router.get_author_books_by_name", new_callable=AsyncMock) as mock:
        mock.side_effect = NotFoundException("No books found")
        response = await async_client.get("/author/books?name=NotAnAuthor")
        assert response.status_code == 404


# ============================================================
# AUTHOR SEARCH
# ============================================================

@pytest.mark.asyncio
async def test_search_authors_returns_200(async_client):
    """Author search endpoint returns 200."""
    with patch("app.api.routes.authors.router.search_authors", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_AUTHOR]
        response = await async_client.get("/author/search?name=Frank+Herbert")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_search_authors_requires_name(async_client):
    """Author search endpoint requires name parameter."""
    response = await async_client.get("/author/search")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_search_authors_returns_list(async_client):
    """Author search endpoint returns a list."""
    with patch("app.api.routes.authors.router.search_authors", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_AUTHOR]
        response = await async_client.get("/author/search?name=Frank+Herbert")
        assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_search_authors_returns_empty_list_when_none_found(async_client):
    """Author search endpoint returns empty list when no authors found."""
    with patch("app.api.routes.authors.router.search_authors", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/author/search?name=NotAnAuthor")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.asyncio
async def test_search_authors_all_regions(async_client):
    """Author search works for all supported regions."""
    regions = ["us", "uk", "ca", "au", "de", "fr", "it", "es", "jp", "in", "br"]
    with patch("app.api.routes.authors.router.search_authors", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_AUTHOR]
        for region in regions:
            response = await async_client.get(f"/author/search?name=Frank+Herbert&region={region}")
            assert response.status_code == 200, f"Failed for region: {region}"