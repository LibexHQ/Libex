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
    "skuGroup": "BK_ADBL_002663",
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

MOCK_AUTHOR = {
    "id": 1,
    "asin": "B000APF21M",
    "name": "Frank Herbert",
    "description": "Frank Herbert was an American science fiction author.",
    "image": "https://example.com/frank-herbert.jpg",
    "region": "us",
    "regions": ["us"],
    "genres": [],
    "updatedAt": "2024-01-01T00:00:00+00:00",
}

MOCK_SERIES = {
    "asin": "B00SERIES1",
    "name": "Dune Chronicles",
    "description": "The Dune Chronicles is a science fiction series.",
    "region": "us",
    "position": None,
    "updatedAt": None,
}

MOCK_CHAPTERS = {
    "brandIntroDurationMs": 0,
    "brandOutroDurationMs": 0,
    "chapters": [
        {"lengthMs": 1200000, "startOffsetMs": 0, "startOffsetSec": 0, "title": "Opening Credits"},
        {"lengthMs": 3600000, "startOffsetMs": 1200000, "startOffsetSec": 1200, "title": "Chapter 1"},
    ],
    "isAccurate": True,
    "runtimeLengthMs": 4800000,
    "runtimeLengthSec": 4800,
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


# ============================================================
# GET /db/book/{asin}
# ============================================================

@pytest.mark.asyncio
async def test_get_db_book_returns_200(async_client):
    """Returns 200 with valid ASIN."""
    with patch("app.api.routes.db.router.get_book_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_BOOK
        response = await async_client.get("/db/book/B08G9PRS1K")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_book_returns_correct_asin(async_client):
    """Returns book with the requested ASIN."""
    with patch("app.api.routes.db.router.get_book_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_BOOK
        response = await async_client.get("/db/book/B08G9PRS1K")
        assert response.json()["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_db_book_not_found_returns_404(async_client):
    """Returns 404 when book not in local DB."""
    with patch("app.api.routes.db.router.get_book_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = None
        response = await async_client.get("/db/book/B08G9PRS1K")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_db_book_rejects_invalid_asin(async_client):
    """Returns 404 with error message for invalid ASIN."""
    response = await async_client.get("/db/book/not-an-asin")
    assert response.status_code == 404
    assert "Invalid ASIN" in response.json()["error"]


@pytest.mark.asyncio
async def test_get_db_book_forwards_asin_to_reader(async_client):
    """ASIN is forwarded to get_book_from_db."""
    with patch("app.api.routes.db.router.get_book_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_BOOK
        await async_client.get("/db/book/B08G9PRS1K")
        args, _ = mock.call_args
        assert args[1] == "B08G9PRS1K"


# ============================================================
# GET /db/book/{asin}/chapters
# ============================================================

@pytest.mark.asyncio
async def test_get_db_book_chapters_returns_200(async_client):
    """Returns 200 when chapter data exists."""
    with patch("app.api.routes.db.router.get_track_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_CHAPTERS
        response = await async_client.get("/db/book/B08G9PRS1K/chapters")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_book_chapters_returns_chapter_data(async_client):
    """Returns the raw chapter JSONB dict."""
    with patch("app.api.routes.db.router.get_track_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_CHAPTERS
        response = await async_client.get("/db/book/B08G9PRS1K/chapters")
        data = response.json()
        assert "chapters" in data
        assert isinstance(data["chapters"], list)


@pytest.mark.asyncio
async def test_get_db_book_chapters_not_found_returns_404(async_client):
    """Returns 404 when no chapter data in local DB."""
    with patch("app.api.routes.db.router.get_track_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = None
        response = await async_client.get("/db/book/B08G9PRS1K/chapters")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_db_book_chapters_rejects_invalid_asin(async_client):
    """Returns 404 with error message for invalid ASIN."""
    response = await async_client.get("/db/book/not-an-asin/chapters")
    assert response.status_code == 404
    assert "Invalid ASIN" in response.json()["error"]


# ============================================================
# GET /db/book/sku/{sku}
# ============================================================

@pytest.mark.asyncio
async def test_get_db_books_by_sku_returns_200(async_client):
    """Returns 200 with valid SKU."""
    with patch("app.api.routes.db.router.get_books_by_sku_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/book/sku/BK_ADBL_002663")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_books_by_sku_returns_list(async_client):
    """Returns a list of BookResponse objects."""
    with patch("app.api.routes.db.router.get_books_by_sku_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/book/sku/BK_ADBL_002663")
        data = response.json()
        assert isinstance(data, list)
        assert data[0]["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_db_books_by_sku_not_found_returns_404(async_client):
    """Returns 404 when no books found for SKU."""
    with patch("app.api.routes.db.router.get_books_by_sku_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/db/book/sku/BK_FAKE_000000")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_db_books_by_sku_forwards_sku_to_reader(async_client):
    """SKU value is forwarded to get_books_by_sku_from_db."""
    with patch("app.api.routes.db.router.get_books_by_sku_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/db/book/sku/BK_ADBL_002663")
        args, _ = mock.call_args
        assert args[1] == "BK_ADBL_002663"


# ============================================================
# GET /db/author/{asin}
# ============================================================

@pytest.mark.asyncio
async def test_get_db_author_returns_200(async_client):
    """Returns 200 with valid ASIN."""
    with patch("app.api.routes.db.router.get_author_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_AUTHOR
        response = await async_client.get("/db/author/B000APF21M")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_author_returns_correct_asin(async_client):
    """Returns author with the requested ASIN."""
    with patch("app.api.routes.db.router.get_author_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_AUTHOR
        response = await async_client.get("/db/author/B000APF21M")
        assert response.json()["asin"] == "B000APF21M"


@pytest.mark.asyncio
async def test_get_db_author_returns_required_fields(async_client):
    """Returns all required AuthorResponse fields."""
    with patch("app.api.routes.db.router.get_author_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_AUTHOR
        response = await async_client.get("/db/author/B000APF21M")
        data = response.json()
        for field in ["asin", "name", "region", "regions", "genres"]:
            assert field in data, f"Missing required field: {field}"


@pytest.mark.asyncio
async def test_get_db_author_not_found_returns_404(async_client):
    """Returns 404 when author not in local DB."""
    with patch("app.api.routes.db.router.get_author_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = None
        response = await async_client.get("/db/author/B000APF21M")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_db_author_rejects_invalid_asin(async_client):
    """Returns 404 with error message for invalid ASIN."""
    response = await async_client.get("/db/author/not-an-asin")
    assert response.status_code == 404
    assert "Invalid ASIN" in response.json()["error"]


@pytest.mark.asyncio
async def test_get_db_author_default_region_is_us(async_client):
    """Defaults to US region when not specified."""
    with patch("app.api.routes.db.router.get_author_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_AUTHOR
        await async_client.get("/db/author/B000APF21M")
        args, _ = mock.call_args
        assert args[2] == "us"


@pytest.mark.asyncio
@pytest.mark.parametrize("region", ["us", "uk", "ca", "au", "de", "fr", "it", "es", "jp", "in", "br"])
async def test_get_db_author_all_regions(async_client, region):
    """Works for all supported regions."""
    with patch("app.api.routes.db.router.get_author_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = {**MOCK_AUTHOR, "region": region, "regions": [region]}
        response = await async_client.get(f"/db/author/B000APF21M?region={region}")
        assert response.status_code == 200, f"Failed for region: {region}"


@pytest.mark.asyncio
async def test_get_db_author_forwards_region_to_reader(async_client):
    """Region parameter is forwarded to get_author_from_db."""
    with patch("app.api.routes.db.router.get_author_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = {**MOCK_AUTHOR, "region": "uk", "regions": ["uk"]}
        await async_client.get("/db/author/B000APF21M?region=uk")
        args, _ = mock.call_args
        assert args[2] == "uk"


# ============================================================
# GET /db/author/{asin}/books
# ============================================================

@pytest.mark.asyncio
async def test_get_db_author_books_returns_200(async_client):
    """Returns 200 with valid ASIN."""
    with patch("app.api.routes.db.router.get_author_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/author/B000APF21M/books")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_author_books_returns_list(async_client):
    """Returns a list of BookResponse objects."""
    with patch("app.api.routes.db.router.get_author_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/author/B000APF21M/books")
        data = response.json()
        assert isinstance(data, list)
        assert data[0]["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_db_author_books_not_found_returns_404(async_client):
    """Returns 404 when no books found for author."""
    with patch("app.api.routes.db.router.get_author_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/db/author/B000APF21M/books")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_db_author_books_rejects_invalid_asin(async_client):
    """Returns 404 with error message for invalid ASIN."""
    response = await async_client.get("/db/author/not-an-asin/books")
    assert response.status_code == 404
    assert "Invalid ASIN" in response.json()["error"]


@pytest.mark.asyncio
async def test_get_db_author_books_default_region_is_us(async_client):
    """Defaults to US region when not specified."""
    with patch("app.api.routes.db.router.get_author_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/db/author/B000APF21M/books")
        args, _ = mock.call_args
        assert args[2] == "us"


@pytest.mark.asyncio
@pytest.mark.parametrize("region", ["us", "uk", "ca", "au", "de", "fr", "it", "es", "jp", "in", "br"])
async def test_get_db_author_books_all_regions(async_client, region):
    """Works for all supported regions."""
    with patch("app.api.routes.db.router.get_author_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get(f"/db/author/B000APF21M/books?region={region}")
        assert response.status_code == 200, f"Failed for region: {region}"


@pytest.mark.asyncio
async def test_get_db_author_books_forwards_region_to_reader(async_client):
    """Region parameter is forwarded to get_author_books_from_db."""
    with patch("app.api.routes.db.router.get_author_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/db/author/B000APF21M/books?region=uk")
        args, _ = mock.call_args
        assert args[2] == "uk"


# ============================================================
# GET /db/series/{asin}
# ============================================================

@pytest.mark.asyncio
async def test_get_db_series_returns_200(async_client):
    """Returns 200 with valid ASIN."""
    with patch("app.api.routes.db.router.get_series_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_SERIES
        response = await async_client.get("/db/series/B00SERIES1")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_series_returns_correct_asin(async_client):
    """Returns series with the requested ASIN."""
    with patch("app.api.routes.db.router.get_series_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_SERIES
        response = await async_client.get("/db/series/B00SERIES1")
        assert response.json()["asin"] == "B00SERIES1"


@pytest.mark.asyncio
async def test_get_db_series_returns_required_fields(async_client):
    """Returns all required SeriesResponse fields."""
    with patch("app.api.routes.db.router.get_series_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_SERIES
        response = await async_client.get("/db/series/B00SERIES1")
        data = response.json()
        for field in ["asin", "name", "region"]:
            assert field in data, f"Missing required field: {field}"


@pytest.mark.asyncio
async def test_get_db_series_not_found_returns_404(async_client):
    """Returns 404 when series not in local DB."""
    with patch("app.api.routes.db.router.get_series_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = None
        response = await async_client.get("/db/series/B00SERIES1")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_db_series_rejects_invalid_asin(async_client):
    """Returns 404 with error message for invalid ASIN."""
    response = await async_client.get("/db/series/not-an-asin")
    assert response.status_code == 404
    assert "Invalid ASIN" in response.json()["error"]


@pytest.mark.asyncio
async def test_get_db_series_forwards_asin_to_reader(async_client):
    """ASIN is forwarded to get_series_from_db."""
    with patch("app.api.routes.db.router.get_series_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_SERIES
        await async_client.get("/db/series/B00SERIES1")
        args, _ = mock.call_args
        assert args[1] == "B00SERIES1"


# ============================================================
# GET /db/series/{asin}/books
# ============================================================

@pytest.mark.asyncio
async def test_get_db_series_books_returns_200(async_client):
    """Returns 200 with valid ASIN."""
    with patch("app.api.routes.db.router.get_series_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/series/B00SERIES1/books")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_series_books_returns_list(async_client):
    """Returns a list of BookResponse objects."""
    with patch("app.api.routes.db.router.get_series_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/series/B00SERIES1/books")
        data = response.json()
        assert isinstance(data, list)
        assert data[0]["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_db_series_books_not_found_returns_404(async_client):
    """Returns 404 when no books found for series."""
    with patch("app.api.routes.db.router.get_series_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/db/series/B00SERIES1/books")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_db_series_books_rejects_invalid_asin(async_client):
    """Returns 404 with error message for invalid ASIN."""
    response = await async_client.get("/db/series/not-an-asin/books")
    assert response.status_code == 404
    assert "Invalid ASIN" in response.json()["error"]


@pytest.mark.asyncio
async def test_get_db_series_books_forwards_asin_to_reader(async_client):
    """ASIN is forwarded to get_series_books_from_db."""
    with patch("app.api.routes.db.router.get_series_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/db/series/B00SERIES1/books")
        args, _ = mock.call_args
        assert args[1] == "B00SERIES1"


@pytest.mark.asyncio
async def test_get_db_series_books_returns_multiple(async_client):
    """Returns multiple books when series has more than one."""
    mock_book_2 = {**MOCK_BOOK, "asin": "B08G9PRS2K", "title": "Test Book 2"}
    with patch("app.api.routes.db.router.get_series_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK, mock_book_2]
        response = await async_client.get("/db/series/B00SERIES1/books")
        data = response.json()
        assert len(data) == 2
        assert data[0]["asin"] == "B08G9PRS1K"
        assert data[1]["asin"] == "B08G9PRS2K"