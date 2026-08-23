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


@pytest.mark.asyncio
async def test_get_narrator_books_defaults_page_to_zero(async_client):
    """Omitting page reaches search() as page 0."""
    with patch("app.api.routes.narrators.router.search", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/narrator/books?name=Scott+Brick&region=us")
        _, kwargs = mock.call_args
        assert kwargs["page"] == 0


@pytest.mark.asyncio
async def test_get_narrator_books_passes_page_to_search(async_client):
    """A non-default page reaches search() with the value the caller sent."""
    with patch("app.api.routes.narrators.router.search", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/narrator/books?name=Scott+Brick&region=us&page=9")
        _, kwargs = mock.call_args
        assert kwargs["page"] == 9


@pytest.mark.asyncio
async def test_get_narrator_books_rejects_negative_page(async_client):
    """page=-1 is rejected."""
    response = await async_client.get("/narrator/books?name=Scott+Brick&region=us&page=-1")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_narrator_books_rejects_page_above_nine(async_client):
    """page=10 is rejected -- Audible re-serves page 9's results as page 10, so
    the upper bound stops duplicates being handed back as new pages."""
    response = await async_client.get("/narrator/books?name=Scott+Brick&region=us&page=10")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_narrator_books_omitted_page_behaves_as_before(async_client):
    """A call omitting page returns 200 and the expected list, unchanged from
    the pre-pagination behaviour."""
    with patch("app.api.routes.narrators.router.search", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/narrator/books?name=Scott+Brick&region=us")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert data[0]["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_narrator_books_rejects_limit_above_fifty(async_client):
    """limit is still rejected above 50."""
    response = await async_client.get("/narrator/books?name=Scott+Brick&region=us&limit=51")
    assert response.status_code == 422