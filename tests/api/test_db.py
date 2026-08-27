"""
DB query endpoint tests.
Tests route validation, filtering, pagination, and error handling.
DB reader is mocked — we test our routing logic not SQLAlchemy.
"""

# Standard library
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

# Third party
import re

import pytest
from httpx import AsyncClient, ASGITransport

# Local
from app.main import app
import app.api.routes.large_response as large_response_module
from app.api.routes.large_response import LARGE_RESPONSE_THREAD_THRESHOLD
from app.services.db.reader import DbStatsResult


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

MOCK_STATS = {
    "books": 150, "authors": 42, "narrators": 85, "series": 18, "booksWithChapters": 7,
}


def _stats_result(stats=None, seconds=300):
    """A DbStatsResult with an expiry `seconds` in the future -- the shape
    get_db_stats now always returns. `seconds=None` produces the no-store
    case (cache_expires_at=None): the DB-failure fallback or a cache-write
    failure after an otherwise successful query."""
    if seconds is None:
        return DbStatsResult(stats if stats is not None else MOCK_STATS, None)
    return DbStatsResult(
        stats if stats is not None else MOCK_STATS,
        datetime.now(timezone.utc) + timedelta(seconds=seconds),
    )


def _many_db_books(n):
    """n distinct books, cheap enough to build at both below- and
    well-above-threshold sizes for the large-catalogue offload tests."""
    return [{**MOCK_BOOK, "asin": f"B{i:09d}"} for i in range(n)]


# ============================================================
# GET /db/stats
# ============================================================


@pytest.mark.asyncio
async def test_get_db_stats_returns_200(async_client):
    """Returns 200 with stats."""
    with patch("app.api.routes.db.router.get_db_stats", new_callable=AsyncMock) as mock:
        mock.return_value = _stats_result({
            "books": 150, "authors": 42, "narrators": 85, "series": 18, "booksWithChapters": 7,
        })
        response = await async_client.get("/db/stats")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_stats_returns_all_fields(async_client):
    """Returns books, authors, narrators, series, and booksWithChapters counts."""
    with patch("app.api.routes.db.router.get_db_stats", new_callable=AsyncMock) as mock:
        mock.return_value = _stats_result({
            "books": 150, "authors": 42, "narrators": 85, "series": 18, "booksWithChapters": 7,
        })
        response = await async_client.get("/db/stats")
        data = response.json()
        assert data["books"] == 150
        assert data["authors"] == 42
        assert data["narrators"] == 85
        assert data["series"] == 18
        assert data["booksWithChapters"] == 7


@pytest.mark.asyncio
async def test_get_db_stats_returns_zeros_on_empty_db(async_client):
    """Returns zero counts when DB is empty."""
    with patch("app.api.routes.db.router.get_db_stats", new_callable=AsyncMock) as mock:
        mock.return_value = _stats_result({
            "books": 0, "authors": 0, "narrators": 0, "series": 0, "booksWithChapters": 0,
        })
        response = await async_client.get("/db/stats")
        data = response.json()
        assert data["books"] == 0
        assert data["authors"] == 0
        assert data["narrators"] == 0
        assert data["series"] == 0
        assert data["booksWithChapters"] == 0


@pytest.mark.asyncio
async def test_get_db_stats_missing_books_with_chapters_from_service_defaults_to_zero(async_client):
    """
    StatsResponse.booksWithChapters has a default, so a service dict that
    omits the key (e.g. an older fallback) still validates instead of 500ing
    — documents the current additive-field behavior rather than asserting a
    guarantee that a future non-default field would need to uphold.
    """
    with patch("app.api.routes.db.router.get_db_stats", new_callable=AsyncMock) as mock:
        mock.return_value = _stats_result({"books": 150, "authors": 42, "narrators": 85, "series": 18})
        response = await async_client.get("/db/stats")
        assert response.status_code == 200
        assert response.json()["booksWithChapters"] == 0


# ============================================================
# GET /db/stats — region scoping
# ============================================================


@pytest.mark.asyncio
async def test_get_db_stats_no_region_param_forwards_none_to_the_reader(async_client):
    """A plain call with no `region` query param must still forward
    region=None to the reader -- the same call an old client makes."""
    with patch("app.api.routes.db.router.get_db_stats", new_callable=AsyncMock) as mock:
        mock.return_value = _stats_result({
            "books": 150, "authors": 42, "narrators": 85, "series": 18, "booksWithChapters": 7,
        })
        response = await async_client.get("/db/stats")

        assert response.status_code == 200
        assert mock.call_args.args[1] is None


@pytest.mark.asyncio
async def test_get_db_stats_no_region_response_carries_only_the_original_five_stat_values(async_client):
    """
    Documents the actual current shape of a no-param call rather than the
    stronger claim that it is byte-for-byte identical to the pre-region
    response: StatsResponse now always includes a `region` key (null here),
    which a caller ignoring unknown fields tolerates (additive, per the
    drop-in compatibility rule) but which a caller pinning the exact key
    set of the old response would not have seen before this change.
    """
    with patch("app.api.routes.db.router.get_db_stats", new_callable=AsyncMock) as mock:
        mock.return_value = _stats_result({
            "books": 150, "authors": 42, "narrators": 85, "series": 18, "booksWithChapters": 7,
        })
        response = await async_client.get("/db/stats")

        data = response.json()
        assert {"books", "authors", "narrators", "series", "booksWithChapters"} <= set(data.keys())
        assert data["books"] == 150
        assert data["authors"] == 42
        assert data["narrators"] == 85
        assert data["series"] == 18
        assert data["booksWithChapters"] == 7
        assert data["region"] is None


@pytest.mark.asyncio
async def test_get_db_stats_region_param_is_forwarded_normalized(async_client):
    """A region query param must reach the reader only after
    validate_region has run on it -- normalized to lowercase, not the raw
    query string."""
    with patch("app.api.routes.db.router.get_db_stats", new_callable=AsyncMock) as mock:
        mock.return_value = _stats_result({
            "books": 100, "authors": 200, "narrators": 999, "series": 300,
            "booksWithChapters": 400, "seriesRegionUnknown": 5,
        })
        response = await async_client.get("/db/stats?region=UK")

        assert response.status_code == 200
        assert mock.call_args.args[1] == "uk"
        assert response.json()["region"] == "uk"


@pytest.mark.asyncio
async def test_get_db_stats_invalid_region_returns_400_same_shape_as_categories(async_client):
    """
    An invalid region must fail the same way every other region-taking
    endpoint does: the same RegionException, the same {"error", "status_code"}
    body -- not a bespoke 422 from FastAPI's own query validation, and not a
    different error shape just because this route calls validate_region()
    directly instead of going through the valid_region dependency.
    """
    stats_response = await async_client.get("/db/stats?region=zz")
    categories_response = await async_client.get("/categories?region=zz")

    assert stats_response.status_code == 400
    assert stats_response.status_code == categories_response.status_code
    assert stats_response.json() == categories_response.json()
    assert stats_response.json() == {"error": "Invalid region: zz", "status_code": 400}


@pytest.mark.asyncio
async def test_get_db_stats_series_region_unknown_reaches_the_caller_through_the_route(async_client):
    """
    seriesRegionUnknown must survive the round trip through StatsResponse,
    not just the reader. The field is a normal declared field on the model
    (`seriesRegionUnknown: int | None = None`), not a passthrough extra --
    but response_model still only serializes declared fields, so a test
    that only calls get_db_stats() directly would pass even if the field
    were removed from StatsResponse and silently dropped on its way out
    through the route.
    """
    with patch("app.api.routes.db.router.get_db_stats", new_callable=AsyncMock) as mock:
        mock.return_value = _stats_result({
            "books": 100, "authors": 200, "narrators": 999, "series": 300,
            "booksWithChapters": 400, "seriesRegionUnknown": 5,
        })
        response = await async_client.get("/db/stats?region=us")

        assert response.status_code == 200
        assert response.json()["seriesRegionUnknown"] == 5


# ============================================================
# GET /db/stats — Cache-Control header
# ============================================================


@pytest.mark.asyncio
async def test_get_db_stats_unscoped_sets_cache_control_header(async_client):
    """The regression that mattered: this route previously sent no
    Cache-Control at all, which is why Cloudflare reported BYPASS on every
    call. The README's badges fire 37 fetches across 9 distinct origin
    URLs on every render; whether shields.io actually honours this header
    on its own fetch, and so throttles how many of those 37 reach the
    origin, has not been measured. A plain, unscoped call must carry the
    header."""
    with patch("app.api.routes.db.router.get_db_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result()
        response = await async_client.get("/db/stats")

    assert response.status_code == 200
    assert "Cache-Control" in response.headers
    assert response.headers["Cache-Control"].startswith("public,")


@pytest.mark.asyncio
async def test_get_db_stats_scoped_sets_cache_control_header(async_client):
    """Same regression, scoped call -- a region query param must not skip
    the header logic."""
    with patch("app.api.routes.db.router.get_db_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result(MOCK_STATS)
        response = await async_client.get("/db/stats?region=us")

    assert response.status_code == 200
    assert "Cache-Control" in response.headers
    assert response.headers["Cache-Control"].startswith("public,")


@pytest.mark.asyncio
async def test_get_db_stats_max_age_tracks_the_entrys_remaining_life_not_the_flat_ttl(async_client):
    """The whole point of the change: max-age has to quote how much life is
    actually left on the cache entry, not a flat re-quote of
    STATS_CACHE_TTL_SECONDS every time regardless of how far into its life
    the entry already is. A entry primed with ~42 seconds left must produce
    a max-age in that neighborhood, not 300 -- a test that only checked
    "some Cache-Control exists" would pass just as well against a hardcoded
    300 and miss this entirely."""
    with patch("app.api.routes.db.router.get_db_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result(seconds=42)
        response = await async_client.get("/db/stats")

    max_age = int(re.search(r"max-age=(\d+)", response.headers["Cache-Control"]).group(1))
    s_maxage = int(re.search(r"s-maxage=(\d+)", response.headers["Cache-Control"]).group(1))
    assert 40 <= max_age <= 42
    assert max_age == s_maxage
    # Not the flat TTL -- that would mean the real lookup never happened.
    assert max_age != 300


@pytest.mark.asyncio
async def test_get_db_stats_longer_remaining_life_also_tracked_not_flat_ttl(async_client):
    """Same proof at a different TTL, so the first test isn't just a
    coincidence of one magic number: a 99-second entry must not also
    collapse to 300."""
    with patch("app.api.routes.db.router.get_db_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result(MOCK_STATS, seconds=99)
        response = await async_client.get("/db/stats?region=us")

    max_age = int(re.search(r"max-age=(\d+)", response.headers["Cache-Control"]).group(1))
    assert 96 <= max_age <= 99
    assert max_age != 300


@pytest.mark.asyncio
async def test_get_db_stats_db_failure_fallback_gets_no_store(async_client):
    """The regression this change exists to close: the previous version made
    a second, independent cache.get_entry call in the router, which could not
    distinguish "healthy, just written" from "degraded, deliberately not
    written" -- a cache miss there fell back to advertising the full TTL, so
    the all-zeros DB-failure fallback got 'public, max-age=300, s-maxage=300'
    and Cloudflare held those zeros at the edge for five minutes with no way
    for origin to self-correct. get_db_stats now reports this case as
    cache_expires_at=None, and the router must translate that into
    Cache-Control: no-store, never a max-age."""
    with patch("app.api.routes.db.router.get_db_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = DbStatsResult(
            {"books": 0, "authors": 0, "narrators": 0, "series": 0, "booksWithChapters": 0},
            None,
        )
        response = await async_client.get("/db/stats")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "max-age" not in response.headers["Cache-Control"]


@pytest.mark.asyncio
async def test_get_db_stats_cache_write_failure_gets_no_store(async_client):
    """The other case where get_db_stats reports cache_expires_at=None: the
    live query succeeded but the follow-up cache.set failed, so nothing was
    actually persisted for any max-age to describe. Quoting a lifetime for an
    entry that was never written is exactly the bug being fixed -- this must
    also be no-store, never the full TTL this test used to assert."""
    with patch("app.api.routes.db.router.get_db_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = DbStatsResult(MOCK_STATS, None)
        response = await async_client.get("/db/stats")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "max-age" not in response.headers["Cache-Control"]


@pytest.mark.asyncio
async def test_get_db_stats_entry_on_the_edge_of_expiry_never_advertises_a_negative_max_age(async_client):
    """An entry can lapse between whatever wrote it and this arithmetic
    running. A negative max-age is malformed and a caching proxy may treat
    it unpredictably -- this must clamp to zero, not go negative."""
    with patch("app.api.routes.db.router.get_db_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = _stats_result(seconds=-5)
        response = await async_client.get("/db/stats")

    assert "max-age=0" in response.headers["Cache-Control"]
    assert "max-age=-" not in response.headers["Cache-Control"]


@pytest.mark.asyncio
async def test_get_db_stats_scoped_and_unscoped_read_different_cache_entries(async_client):
    """Scoped and unscoped calls are backed by their own cache entries
    (`db_stats` vs `db_stats:<region>`), so their headers can legitimately
    diverge -- a test that expected one value for both would be wrong. Since
    get_db_stats now carries the expiry out of the one cache read/write it
    already performs, this proves the router's header tracks whichever
    result it was actually handed, per call, rather than a single shared
    value."""
    results = {
        None: _stats_result(seconds=42),
        "us": _stats_result(seconds=200),
    }

    async def fake_get_db_stats(session, region=None):
        return results[region]

    with patch("app.api.routes.db.router.get_db_stats", side_effect=fake_get_db_stats):
        unscoped_response = await async_client.get("/db/stats")
        scoped_response = await async_client.get("/db/stats?region=us")

    unscoped_max_age = int(re.search(r"max-age=(\d+)", unscoped_response.headers["Cache-Control"]).group(1))
    scoped_max_age = int(re.search(r"max-age=(\d+)", scoped_response.headers["Cache-Control"]).group(1))
    assert 40 <= unscoped_max_age <= 42
    assert 198 <= scoped_max_age <= 200
    assert unscoped_max_age != scoped_max_age


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
    ("plan_name", "US Minerva", "plan_name", "US Minerva"),
    ("category", "18580628011", "category", "18580628011"),
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


@pytest.mark.asyncio
async def test_plan_name_filter_returns_200(async_client):
    """Returns 200 with plan_name filter."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/book?plan_name=US+Minerva")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_plan_name_filter_forwarded_to_reader(async_client):
    """plan_name parameter is forwarded to search_books_from_db."""
    with patch(READER_PATH, new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/db/book?plan_name=AccessViaMusic")
        _, kwargs = mock.call_args
        assert kwargs["plan_name"] == "AccessViaMusic"


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
# GET /db/plans
# ============================================================

@pytest.mark.asyncio
async def test_get_db_plans_returns_200(async_client):
    """Returns 200 when plans exist."""
    with patch("app.api.routes.db.router.get_distinct_plans_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = ["AccessViaMusic", "US Minerva"]
        response = await async_client.get("/db/plans")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_plans_returns_list_of_strings(async_client):
    """Returns a list of plan name strings."""
    with patch("app.api.routes.db.router.get_distinct_plans_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = ["AccessViaMusic", "US Minerva"]
        response = await async_client.get("/db/plans")
        data = response.json()
        assert isinstance(data, list)
        assert "US Minerva" in data
        assert "AccessViaMusic" in data


@pytest.mark.asyncio
async def test_get_db_plans_not_found_returns_404(async_client):
    """Returns 404 when no plans exist in local DB."""
    with patch("app.api.routes.db.router.get_distinct_plans_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/db/plans")
        assert response.status_code == 404


# ============================================================
# GET /db/plans/{plan_name}
# ============================================================

@pytest.mark.asyncio
async def test_get_db_books_by_plan_returns_200(async_client):
    """Returns 200 with valid plan name."""
    with patch("app.api.routes.db.router.get_books_by_plan_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/plans/US Minerva")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_books_by_plan_returns_list(async_client):
    """Returns a list of BookResponse objects."""
    with patch("app.api.routes.db.router.get_books_by_plan_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/plans/US Minerva")
        data = response.json()
        assert isinstance(data, list)
        assert data[0]["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_db_books_by_plan_not_found_returns_404(async_client):
    """Returns 404 when no books found for plan."""
    with patch("app.api.routes.db.router.get_books_by_plan_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/db/plans/FakePlan")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_db_books_by_plan_forwards_plan_name_to_reader(async_client):
    """Plan name is forwarded to get_books_by_plan_from_db."""
    with patch("app.api.routes.db.router.get_books_by_plan_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/db/plans/AccessViaMusic")
        args, _ = mock.call_args
        assert args[1] == "AccessViaMusic"


@pytest.mark.asyncio
async def test_get_db_books_by_plan_pagination_defaults(async_client):
    """Default limit is 20 and default page is 1."""
    with patch("app.api.routes.db.router.get_books_by_plan_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/db/plans/US Minerva")
        _, kwargs = mock.call_args
        assert kwargs["limit"] == 20
        assert kwargs["page"] == 1


@pytest.mark.asyncio
async def test_get_db_books_by_plan_pagination_forwarded(async_client):
    """Pagination parameters are forwarded to reader."""
    with patch("app.api.routes.db.router.get_books_by_plan_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/db/plans/US Minerva?limit=5&page=3")
        _, kwargs = mock.call_args
        assert kwargs["limit"] == 5
        assert kwargs["page"] == 3


# ============================================================
# GET /db/vvab
# ============================================================

@pytest.mark.asyncio
async def test_get_db_vvab_returns_200(async_client):
    """Returns 200 when VVAB books exist."""
    with patch("app.api.routes.db.router.get_vvab_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/vvab")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_vvab_returns_list(async_client):
    """Returns a list of BookResponse objects."""
    with patch("app.api.routes.db.router.get_vvab_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/vvab")
        data = response.json()
        assert isinstance(data, list)
        assert data[0]["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_db_vvab_not_found_returns_404(async_client):
    """Returns 404 when no VVAB books in local DB."""
    with patch("app.api.routes.db.router.get_vvab_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/db/vvab")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_db_vvab_pagination_defaults(async_client):
    """Default limit is 20 and default page is 1."""
    with patch("app.api.routes.db.router.get_vvab_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/db/vvab")
        _, kwargs = mock.call_args
        assert kwargs["limit"] == 20
        assert kwargs["page"] == 1


@pytest.mark.asyncio
async def test_get_db_vvab_pagination_forwarded(async_client):
    """Pagination parameters are forwarded to reader."""
    with patch("app.api.routes.db.router.get_vvab_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/db/vvab?limit=10&page=2")
        _, kwargs = mock.call_args
        assert kwargs["limit"] == 10
        assert kwargs["page"] == 2


# ============================================================
# GET /db/narrator/books
# ============================================================


@pytest.mark.asyncio
async def test_get_db_narrator_books_returns_200(async_client):
    """Returns 200 when books exist for narrator."""
    with patch("app.api.routes.db.router.get_narrator_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/narrator/books?name=Jim+Dale")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_narrator_books_returns_list(async_client):
    """Returns a list of BookResponse objects."""
    with patch("app.api.routes.db.router.get_narrator_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get("/db/narrator/books?name=Jim+Dale")
        data = response.json()
        assert isinstance(data, list)
        assert data[0]["asin"] == "B08G9PRS1K"


@pytest.mark.asyncio
async def test_get_db_narrator_books_not_found_returns_404(async_client):
    """Returns 404 when no books found for narrator."""
    with patch("app.api.routes.db.router.get_narrator_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/db/narrator/books?name=Nobody")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_db_narrator_books_pagination_forwarded(async_client):
    """Pagination parameters are forwarded to reader."""
    with patch("app.api.routes.db.router.get_narrator_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get("/db/narrator/books?name=Jim+Dale&limit=5&page=3")
        _, kwargs = mock.call_args
        assert kwargs["limit"] == 5
        assert kwargs["page"] == 3


@pytest.mark.asyncio
async def test_get_db_narrator_books_requires_name(async_client):
    """Returns 422 when name param is missing."""
    response = await async_client.get("/db/narrator/books")
    assert response.status_code == 422


# ============================================================
# GET /db/narrator
# ============================================================


@pytest.mark.asyncio
async def test_search_db_narrators_returns_200(async_client):
    """Returns 200 when narrators found."""
    with patch("app.api.routes.db.router.search_narrators_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [{"name": "Jim Dale", "description": None, "image": None, "website": None, "wikipediaUrl": None, "languages": None, "accents": None, "gender": None, "genresNarrated": None, "audiobooksProduced": None, "culturalHeritage": None, "publishers": None, "socialLinks": None, "audioSamples": None, "source": None, "sourceUrl": None, "sourceUpdatedAt": None, "attribution": None, "updatedAt": None}]
        response = await async_client.get("/db/narrator?name=Jim")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_search_db_narrators_returns_list(async_client):
    """Returns a list of narrator objects."""
    with patch("app.api.routes.db.router.search_narrators_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = [{"name": "Jim Dale", "description": None, "image": None, "website": None, "wikipediaUrl": None, "languages": None, "accents": None, "gender": None, "genresNarrated": None, "audiobooksProduced": None, "culturalHeritage": None, "publishers": None, "socialLinks": None, "audioSamples": None, "source": None, "sourceUrl": None, "sourceUpdatedAt": None, "attribution": None, "updatedAt": None}]
        response = await async_client.get("/db/narrator?name=Jim")
        data = response.json()
        assert isinstance(data, list)
        assert data[0]["name"] == "Jim Dale"


@pytest.mark.asyncio
async def test_search_db_narrators_not_found_returns_404(async_client):
    """Returns 404 when no narrators match."""
    with patch("app.api.routes.db.router.search_narrators_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/db/narrator?name=Nobody")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_search_db_narrators_requires_name(async_client):
    """Returns 422 when name param is missing."""
    response = await async_client.get("/db/narrator")
    assert response.status_code == 422


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
# GET /db/author/{asin}/books — LARGE CATALOGUE OFFLOAD
# ============================================================
#
# This route is wired to build_large_list_response (app.api.routes.large_response),
# which above LARGE_RESPONSE_THREAD_THRESHOLD builds and serializes on a worker
# thread and returns a pre-serialized Response instead of a plain list. It is
# also one of the two /db/* list routes with no limit/page — a deliberate,
# stated exception among the paginated /db/* routes, kept complete on purpose.

@pytest.mark.asyncio
async def test_get_db_author_books_below_and_above_threshold_produce_identical_bodies(async_client):
    """Same data, forced down each path by patching the threshold rather than
    building a 200-item fixture twice, so the two runs differ in nothing but
    which path built the response."""
    books = _many_db_books(5)

    with patch("app.api.routes.db.router.get_author_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = books
        inline_response = await async_client.get("/db/author/B000APF21M/books")

    with patch("app.api.routes.db.router.get_author_books_from_db", new_callable=AsyncMock) as mock, \
         patch("app.api.routes.large_response.LARGE_RESPONSE_THREAD_THRESHOLD", 1):
        mock.return_value = books
        offloaded_response = await async_client.get("/db/author/B000APF21M/books")

    assert inline_response.status_code == 200
    assert offloaded_response.status_code == 200
    assert inline_response.content == offloaded_response.content


@pytest.mark.asyncio
async def test_get_db_author_books_stays_inline_below_the_threshold(async_client):
    """Below the threshold, _build_and_serialize (the offload worker-thread
    entry point) must not be called at all — asserted with a spy, not assumed
    from the response alone."""
    books = _many_db_books(5)

    with patch("app.api.routes.db.router.get_author_books_from_db", new_callable=AsyncMock) as mock, \
         patch(
             "app.api.routes.large_response._build_and_serialize",
             wraps=large_response_module._build_and_serialize,
         ) as spy:
        mock.return_value = books
        response = await async_client.get("/db/author/B000APF21M/books")

    spy.assert_not_called()
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_author_books_offloads_at_the_threshold(async_client):
    """At and above the threshold, the offload path is genuinely taken — a
    spy on _build_and_serialize proves it ran, rather than a bytes comparison
    that would pass just as well if both sides secretly took the inline path."""
    n = LARGE_RESPONSE_THREAD_THRESHOLD
    books = _many_db_books(n)

    with patch("app.api.routes.db.router.get_author_books_from_db", new_callable=AsyncMock) as mock, \
         patch(
             "app.api.routes.large_response._build_and_serialize",
             wraps=large_response_module._build_and_serialize,
         ) as spy:
        mock.return_value = books
        response = await async_client.get("/db/author/B000APF21M/books")

    spy.assert_called_once()
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_author_books_no_truncation_above_threshold(async_client):
    """No limit/page on this route — a full catalogue well above the
    threshold comes back complete, not capped to any page size."""
    n = LARGE_RESPONSE_THREAD_THRESHOLD + 50
    books = _many_db_books(n)

    with patch("app.api.routes.db.router.get_author_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = books
        response = await async_client.get("/db/author/B000APF21M/books")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == n
    assert {b["asin"] for b in data} == {b["asin"] for b in books}


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


# ============================================================
# GET /db/series/{asin}/books — LARGE CATALOGUE OFFLOAD
# ============================================================
#
# Twin coverage of the author/books offload tests above — same helper
# (build_large_list_response), same threshold, same no-limit/no-page
# exception, different reader (get_series_books_from_db).

@pytest.mark.asyncio
async def test_get_db_series_books_below_and_above_threshold_produce_identical_bodies(async_client):
    """Same data, forced down each path by patching the threshold rather than
    building a 200-item fixture twice, so the two runs differ in nothing but
    which path built the response."""
    books = _many_db_books(5)

    with patch("app.api.routes.db.router.get_series_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = books
        inline_response = await async_client.get("/db/series/B00SERIES1/books")

    with patch("app.api.routes.db.router.get_series_books_from_db", new_callable=AsyncMock) as mock, \
         patch("app.api.routes.large_response.LARGE_RESPONSE_THREAD_THRESHOLD", 1):
        mock.return_value = books
        offloaded_response = await async_client.get("/db/series/B00SERIES1/books")

    assert inline_response.status_code == 200
    assert offloaded_response.status_code == 200
    assert inline_response.content == offloaded_response.content


@pytest.mark.asyncio
async def test_get_db_series_books_stays_inline_below_the_threshold(async_client):
    """Below the threshold, _build_and_serialize (the offload worker-thread
    entry point) must not be called at all — asserted with a spy, not assumed
    from the response alone."""
    books = _many_db_books(5)

    with patch("app.api.routes.db.router.get_series_books_from_db", new_callable=AsyncMock) as mock, \
         patch(
             "app.api.routes.large_response._build_and_serialize",
             wraps=large_response_module._build_and_serialize,
         ) as spy:
        mock.return_value = books
        response = await async_client.get("/db/series/B00SERIES1/books")

    spy.assert_not_called()
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_series_books_offloads_at_the_threshold(async_client):
    """At and above the threshold, the offload path is genuinely taken — a
    spy on _build_and_serialize proves it ran, rather than a bytes comparison
    that would pass just as well if both sides secretly took the inline path."""
    n = LARGE_RESPONSE_THREAD_THRESHOLD
    books = _many_db_books(n)

    with patch("app.api.routes.db.router.get_series_books_from_db", new_callable=AsyncMock) as mock, \
         patch(
             "app.api.routes.large_response._build_and_serialize",
             wraps=large_response_module._build_and_serialize,
         ) as spy:
        mock.return_value = books
        response = await async_client.get("/db/series/B00SERIES1/books")

    spy.assert_called_once()
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_db_series_books_no_truncation_above_threshold(async_client):
    """No limit/page on this route — a full series list well above the
    threshold comes back complete, not capped to any page size."""
    n = LARGE_RESPONSE_THREAD_THRESHOLD + 50
    books = _many_db_books(n)

    with patch("app.api.routes.db.router.get_series_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = books
        response = await async_client.get("/db/series/B00SERIES1/books")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == n
    assert {b["asin"] for b in data} == {b["asin"] for b in books}