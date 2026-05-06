"""
Narrator route tests.
Tests the Audible-first narrator books endpoint.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest


MOCK_BOOK = {
    "asin": "B08G9PRS1K",
    "title": "Dune",
    "subtitle": None,
    "description": "A science fiction masterpiece.",
    "summary": "Set on the desert planet Arrakis.",
    "region": "us",
    "regions": ["us"],
    "publisher": "Macmillan Audio",
    "copyright": "©1965 Frank Herbert",
    "isbn": "9780000000000",
    "language": "english",
    "rating": 4.8,
    "bookFormat": "unabridged",
    "releaseDate": "2021-03-02T00:00:00+00:00",
    "explicit": False,
    "hasPdf": False,
    "whisperSync": True,
    "imageUrl": "https://example.com/dune.jpg",
    "lengthMinutes": 660,
    "link": "https://audible.com/pd/B08G9PRS1K",
    "contentType": "Product",
    "contentDeliveryType": "SinglePartBook",
    "episodeNumber": None,
    "episodeType": None,
    "sku": "BK_DUNE_000001",
    "skuGroup": "BK_DUNE_000001",
    "isListenable": True,
    "isAvailable": True,
    "isBuyable": True,
    "isVvab": False,
    "plans": [],
    "updatedAt": None,
    "authors": [{"id": None, "asin": "B000APF21M", "name": "Frank Herbert", "region": "us", "regions": ["us"], "image": None, "updatedAt": None}],
    "narrators": [{"name": "Scott Brick", "updatedAt": None}],
    "genres": [],
    "series": [],
}


# ============================================================
# GET /narrator/books
# ============================================================


@pytest.mark.asyncio
async def test_get_narrator_books_returns_200(async_client):
    """Returns 200 when books found for narrator."""
    with patch("app.api.routes.narrators.router.search", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/narrator/books?name=Scott+Brick&region=us")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_narrator_books_returns_list(async_client):
    """Returns a list of BookResponse objects."""
    with patch("app.api.routes.narrators.router.search", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/narrator/books?name=Scott+Brick&region=us")
        data = response.json()
        assert isinstance(data, list)
        assert data[0]["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_narrator_books_not_found_returns_404(async_client):
    """Returns 404 when no books found."""
    with patch("app.api.routes.narrators.router.search", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/narrator/books?name=Nobody&region=us")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_narrator_books_requires_name(async_client):
    """Returns 422 when name param is missing."""
    response = await async_client.get("/narrator/books?region=us")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_narrator_books_passes_narrator_to_search(async_client):
    """Passes narrator name to search service."""
    with patch("app.api.routes.narrators.router.search", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/narrator/books?name=Scott+Brick&region=us")
        _, kwargs = mock.call_args
        assert kwargs["narrator"] == "Scott Brick"