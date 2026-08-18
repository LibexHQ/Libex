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

@pytest.mark.asyncio
async def test_get_books_by_sku_returns_200(async_client):
    """SKU endpoint returns 200 with valid SKU."""
    with patch("app.api.routes.books.router.get_books_by_sku_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/book/sku/BK_ADBL_002663")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_books_by_sku_returns_list(async_client):
    """SKU endpoint returns a list of BookResponse objects."""
    with patch("app.api.routes.books.router.get_books_by_sku_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/book/sku/BK_ADBL_002663")
        data = response.json()
        assert isinstance(data, list)
        assert data[0]["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_books_by_sku_not_found_returns_404(async_client):
    """SKU endpoint returns 404 when no books found."""
    with patch("app.api.routes.books.router.get_books_by_sku_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/book/sku/BK_FAKE_000000")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_books_by_sku_forwards_sku_to_reader(async_client):
    """SKU endpoint passes sku value to get_books_by_sku_from_db."""
    with patch("app.api.routes.books.router.get_books_by_sku_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/book/sku/BK_ADBL_002663")
        args, _ = mock.call_args
        assert args[1] == "BK_ADBL_002663"


# ============================================================
# ?cache=true — THE SERVED RESPONSE, NOT JUST THE SERVICE RETURN VALUE
# ============================================================
# tests/services/test_books_service.py already proves get_books_by_asins'
# own cache-hit branches return the identical dict a live fetch normalizes
# to. What that leaves unproven is the one thing this router actually ships:
# BookResponse(**data) -- a cache hit and a live fetch could still diverge
# once Pydantic gets to coerce, default, or drop fields on the way out, and
# that is the layer drop-in AudiMeta compatibility is actually enforced at.
# These mock one level deeper than the router (audible_get and cache.get,
# not get_book_by_asin itself) so the real service call, the real cache-hit
# branch, and the real response_model serialization all run for real.

def _cache_route_product(asin):
    """A product shape rich enough to exercise more than the placeholder
    defaults MOCK_BOOK above already carries as None/False."""
    return {
        "asin": asin, "title": "Cache Parity Book", "authors": [], "narrators": [],
        "relationships": [], "product_images": {}, "category_ladders": [],
        "rating": {}, "publication_datetime": "2021-01-01T00:00:00Z",
        "is_listenable": True, "is_buyable": True, "is_vvab": False,
        "plans": [{"plan_name": "US Minerva"}],
    }


@pytest.mark.asyncio
async def test_cache_hit_serves_the_identical_response_body_a_live_fetch_would(async_client):
    """A cache hit and a live fetch of the same ASIN must produce byte-for-byte
    the same served JSON once both have gone through BookResponse -- not just
    the same pre-serialization dict. audible_get is asserted not-called on the
    cache-hit request so this is really the cache branch and not a live fetch
    that happened to agree with itself.

    The cache is seeded with the exact dict the service layer's own
    normalization produced for a live call (obtained by calling
    get_books_by_asins directly, the same function the route calls
    internally) rather than a hand-built stand-in that could drift from what
    normalization genuinely emits -- the service-layer tests already cover
    that parity; what this adds is that BookResponse(**data) serializes both
    paths' payloads to the same served JSON."""
    from app.services.audible.books import get_books_by_asins

    asin = "B0CACHE001"

    with patch("app.services.audible.books.audible_get",
               return_value={"product": _cache_route_product(asin)}), \
         patch("app.services.audible.books.persist_books_background"), \
         patch("app.services.audible.books.cache.get", return_value=None):
        live_response = await async_client.get(f"/book/{asin}")
        normalized = (await get_books_by_asins([asin], "us", AsyncMock()))[0]
    assert live_response.status_code == 200
    live_body = live_response.json()

    with patch("app.services.audible.books.audible_get", new_callable=AsyncMock) as mock_audible_get, \
         patch("app.services.audible.books.cache.get", new=AsyncMock(return_value=normalized)):
        cached_response = await async_client.get(f"/book/{asin}?cache=true")

    mock_audible_get.assert_not_called()
    assert cached_response.status_code == 200
    assert cached_response.json() == live_body