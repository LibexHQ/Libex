"""
DB query endpoint tests.
Tests route validation, filtering, pagination, and error handling.
DB reader is mocked — we test our routing logic not SQLAlchemy.
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
    "isbn": "9780000000000",
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
    "contentDeliveryType": "SinglePartBook",
    "episodeNumber": None,
    "episodeType": None,
    "sku": None,
    "skuGroup": None,
    "isListenable": True,
    "isAvailable": True,
    "isBuyable": True,
    "updatedAt": None,
    "authors": [
        {
            "id": 1,
            "asin": "B000TEST01",
            "name": "Test Author",
            "region": "us",
            "regions": ["us"],
            "image": None,
            "updatedAt": None,
        }
    ],
    "narrators": [{"name": "Test Narrator", "updatedAt": None}],
    "genres": [
        {
            "asin": None,
            "name": "Fiction",
            "type": "Genres",
            "betterType": "genre",
            "updatedAt": None,
        }
    ],
    "series": [],
}

READER_PATH = "app.api.routes.db.router.search_books_from_db"


# ============================================================
# VALIDATION TESTS
# ============================================================


@pytest.mark.asyncio
async def test_no_params_returns_404(async_client):
    """Returns 404 when no filter parameters are provided."""
    response = await async_client.get("/db/book")
    assert response.status_code == 404
    assert "No search parameters provided" in response.json()["error"]


@pytest.mark.asyncio
async def test_only_pagination_params_returns_404(async_client):
    """Returns 404 when only limit/page are provided (not meaningful filters)."""
    response = await async_client.get("/db/book?limit=10&page=2")
    assert response.status_code == 404
    assert "No search parameters provided" in response.json()["error"]


@pytest.mark.asyncio
async def test_limit_above_100_returns_422(async_client):
    """Returns 422 when limit exceeds maximum of 100."""
    response = await async_client.get("/db/book?title=test&limit=101")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_page_below_1_returns_422(async_client):
    """Returns 422 when page is less than 1."""
    response = await async_client.get("/db/book?title=test&page=0")
    assert response.status_code == 422


# ============================================================
# SUCCESS TESTS
# ============================================================


@pytest.mark.asyncio
async def test_title_filter_returns_200(async_client):
    """Returns 200 with title filter."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/book?title=Test+Book")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_isbn_filter_returns_200(async_client):
    """Returns 200 with isbn filter."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/book?isbn=9780000000000")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_response_is_list_of_book_responses(async_client):
    """Response body is a list of BookResponse objects."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/book?title=Test")
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_response_has_required_book_fields(async_client):
    """Each result contains all required BookResponse fields."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/book?title=Test")
        data = response.json()[0]
        required_fields = [
            "asin", "title", "region", "authors", "narrators",
            "genres", "series", "imageUrl", "description",
        ]
        for field in required_fields:
            assert field in data, f"Missing required field: {field}"


@pytest.mark.asyncio
async def test_empty_results_returns_404(async_client):
    """Returns 404 when no books match the filters."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/db/book?title=DoesNotExist")
        assert response.status_code == 404


# ============================================================
# PAGINATION TESTS
# ============================================================


@pytest.mark.asyncio
async def test_pagination_passes_limit_to_reader(async_client):
    """limit parameter is forwarded to search_books_from_db."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/db/book?title=Test&limit=5")
        _, kwargs = mock.call_args
        assert kwargs["limit"] == 5


@pytest.mark.asyncio
async def test_pagination_passes_page_to_reader(async_client):
    """page parameter is forwarded to search_books_from_db."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/db/book?title=Test&page=3")
        _, kwargs = mock.call_args
        assert kwargs["page"] == 3


@pytest.mark.asyncio
async def test_pagination_defaults(async_client):
    """Default limit is 20 and default page is 1."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/db/book?title=Test")
        _, kwargs = mock.call_args
        assert kwargs["limit"] == 20
        assert kwargs["page"] == 1


# ============================================================
# INDIVIDUAL FILTER TESTS
# ============================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("param,value,kwarg,expected", [
    ("title", "Dune", "title", "Dune"),
    ("subtitle", "Messiah", "subtitle", "Messiah"),
    ("region", "us", "region", "us"),
    ("description", "epic", "description", "epic"),
    ("summary", "hero", "summary", "hero"),
    ("publisher", "Macmillan", "publisher", "Macmillan"),
    ("copyright", "2021", "copyright", "2021"),
    ("isbn", "9780000000000", "isbn", "9780000000000"),
    ("author_name", "Frank Herbert", "author_name", "Frank Herbert"),
    ("series_name", "Dune", "series_name", "Dune"),
    ("language", "english", "language", "english"),
    ("book_format", "unabridged", "book_format", "unabridged"),
    ("content_type", "Book", "content_type", "Book"),
    ("content_delivery_type", "SinglePartBook", "content_delivery_type", "SinglePartBook"),
])
async def test_string_filter_forwarded_to_reader(async_client, param, value, kwarg, expected):
    """String filter parameters are forwarded correctly to search_books_from_db."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get(f"/db/book?{param}={value}")
        _, kwargs = mock.call_args
        assert kwargs[kwarg] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("param,value,kwarg,expected", [
    ("rating_better_than", "4.0", "rating_better_than", 4.0),
    ("rating_worse_than", "3.0", "rating_worse_than", 3.0),
    ("longer_than", "60", "longer_than", 60),
    ("shorter_than", "600", "shorter_than", 600),
])
async def test_numeric_filter_forwarded_to_reader(async_client, param, value, kwarg, expected):
    """Numeric filter parameters are forwarded correctly to search_books_from_db."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get(f"/db/book?{param}={value}")
        _, kwargs = mock.call_args
        assert kwargs[kwarg] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("param,kwarg", [
    ("explicit", "explicit"),
    ("whisper_sync", "whisper_sync"),
    ("has_pdf", "has_pdf"),
    ("is_listenable", "is_listenable"),
    ("is_buyable", "is_buyable"),
])
async def test_bool_filter_true_forwarded_to_reader(async_client, param, kwarg):
    """Boolean filter parameters forwarded as True when set to true."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get(f"/db/book?{param}=true")
        _, kwargs = mock.call_args
        assert kwargs[kwarg] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("param,kwarg", [
    ("explicit", "explicit"),
    ("whisper_sync", "whisper_sync"),
    ("has_pdf", "has_pdf"),
    ("is_listenable", "is_listenable"),
    ("is_buyable", "is_buyable"),
])
async def test_bool_filter_false_forwarded_to_reader(async_client, param, kwarg):
    """Boolean filter parameters forwarded as False when set to false (not skipped)."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get(f"/db/book?{param}=false")
        _, kwargs = mock.call_args
        assert kwargs[kwarg] is False


@pytest.mark.asyncio
async def test_bool_false_param_counts_as_meaningful_filter(async_client):
    """A bool param set to false is a valid filter — should not raise 404 for missing params."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/book?explicit=false")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_multiple_filters_all_forwarded(async_client):
    """Multiple filters are all forwarded to the reader together."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/db/book?title=Dune&region=us&rating_better_than=4.0&longer_than=300")
        _, kwargs = mock.call_args
        assert kwargs["title"] == "Dune"
        assert kwargs["region"] == "us"
        assert kwargs["rating_better_than"] == 4.0
        assert kwargs["longer_than"] == 300

@pytest.mark.asyncio
async def test_author_name_filter_returns_200(async_client):
    """Returns 200 with author_name filter."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/book?author_name=Frank+Herbert")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_series_name_filter_returns_200(async_client):
    """Returns 200 with series_name filter."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/book?series_name=Dune")
        assert response.status_code == 200