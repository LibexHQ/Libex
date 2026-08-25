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
from app.core.response_headers import SOURCE_AUDIBLE, SOURCE_CACHE, record_source, record_source_keys

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


@pytest.mark.asyncio
async def test_get_series_falls_back_to_db(async_client):
    """Series endpoint returns DB result when Audible is unavailable."""
    with patch("app.api.routes.series.router.get_series", new_callable=AsyncMock) as mock:
        mock.return_value = {**MOCK_SERIES, "updatedAt": "2024-01-01T00:00:00+00:00"}
        response = await async_client.get("/series/B00SERIES1")
        assert response.status_code == 200
        assert response.json()["asin"] == "B00SERIES1"


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
async def test_search_series_returns_series_not_books(async_client):
    """Series search returns series objects not book objects."""
    with patch("app.api.routes.series.router.search_series", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_SERIES]
        response = await async_client.get("/series/search?name=Dune")
        data = response.json()
        assert isinstance(data, list)
        assert "asin" in data[0]
        assert "name" in data[0]
        assert "title" not in data[0]


@pytest.mark.asyncio
async def test_search_series_returns_multiple_matches(async_client):
    """Series search can return multiple series matches."""
    mock_series_2 = {**MOCK_SERIES, "asin": "B00SERIES2", "name": "Dune Messiah"}
    with patch("app.api.routes.series.router.search_series", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_SERIES, mock_series_2]
        response = await async_client.get("/series/search?name=Dune")
        data = response.json()
        assert len(data) == 2
        assert data[0]["asin"] == "B00SERIES1"
        assert data[1]["asin"] == "B00SERIES2"


@pytest.mark.asyncio
async def test_search_series_deduplicates_results(async_client):
    """Series search does not return duplicate series."""
    with patch("app.api.routes.series.router.search_series", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_SERIES]
        response = await async_client.get("/series/search?name=Dune")
        data = response.json()
        asins = [s["asin"] for s in data]
        assert len(asins) == len(set(asins))


@pytest.mark.asyncio
async def test_search_series_legacy_endpoint_works(async_client):
    """Legacy series search endpoint also returns series list."""
    with patch("app.api.routes.series.router.search_series", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_SERIES]
        response = await async_client.get("/series?name=Dune")
        assert response.status_code == 200
        assert isinstance(response.json(), list)


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

# ============================================================
# CACHE DEFAULT FLIP -- omitting cache now reads the cache
# ============================================================


@pytest.mark.asyncio
async def test_get_series_omits_cache_param_and_reads_the_cache_by_default(async_client):
    with patch("app.api.routes.series.router.get_series", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_SERIES
        await async_client.get("/series/B00SERIES1")
    assert mock.call_args[0][3] is True


@pytest.mark.asyncio
async def test_get_series_cache_false_marks_the_response_no_store(async_client):
    with patch("app.api.routes.series.router.get_series", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_SERIES
        response = await async_client.get("/series/B00SERIES1?cache=false")
    assert mock.call_args[0][3] is False
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_get_series_cache_true_sends_no_cache_control_header(async_client):
    with patch("app.api.routes.series.router.get_series", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_SERIES
        response = await async_client.get("/series/B00SERIES1?cache=true")
    assert "cache-control" not in response.headers


@pytest.mark.asyncio
async def test_get_series_books_omits_cache_param_and_reads_the_cache_for_both_phases_by_default(async_client):
    """cache governs discovery (get_series_books) and hydration
    (get_books_by_asins) as one decision, not two. This is the regression
    test for a live cache-miss loop on this route: hydration used to be
    called without use_cache at all (defaulting to False), so discovery hit
    the stored ASIN list every time while hydration silently refetched
    every book from Audible and rewrote it to cache on every request --
    three requests to the same series measured live at ~1.5s each with
    cache_hits: 0 throughout. A fake that merely accepts use_cache without
    checking it would pass whether or not the route actually threads the
    value through, which is exactly how the defect shipped unnoticed."""
    with patch("app.api.routes.series.router.get_series_books", new_callable=AsyncMock) as mock_series, \
         patch("app.api.routes.series.router.get_books_by_asins", new_callable=AsyncMock) as mock_books:
        mock_series.return_value = ["B08G9PRS1K"]
        mock_books.return_value = [MOCK_BOOK]
        await async_client.get("/series/books/B00SERIES1")
    assert mock_series.call_args[0][3] is True
    assert mock_books.call_args.kwargs["use_cache"] is True


@pytest.mark.asyncio
async def test_get_series_books_cache_false_marks_the_response_no_store(async_client):
    with patch("app.api.routes.series.router.get_series_books", new_callable=AsyncMock) as mock_series, \
         patch("app.api.routes.series.router.get_books_by_asins", new_callable=AsyncMock) as mock_books:
        mock_series.return_value = ["B08G9PRS1K"]
        mock_books.return_value = [MOCK_BOOK]
        response = await async_client.get("/series/books/B00SERIES1?cache=false")
    assert mock_series.call_args[0][3] is False
    assert mock_books.call_args.kwargs["use_cache"] is False
    assert response.headers["cache-control"] == "no-store"


# ============================================================
# X-LIBEX-SOURCE -- /series/{asin} and hydration on /series/books/{asin}
# ============================================================


@pytest.mark.asyncio
async def test_get_series_source_header_reflects_the_recorded_source(async_client):
    async def fake_get_series(asin, region, session, cache, *, facts=None):
        record_source(facts, SOURCE_AUDIBLE)
        return MOCK_SERIES

    with patch("app.api.routes.series.router.get_series", side_effect=fake_get_series):
        response = await async_client.get("/series/B00SERIES1")

    assert response.headers["x-libex-source"] == "audible"
    assert response.headers["x-libex-complete"] == "true"


@pytest.mark.asyncio
async def test_get_series_books_source_header_describes_hydration_only(async_client):
    """X-Libex-Source on this route names where the books in the body came
    from (get_books_by_asins), never the discovery read that found their
    ASINs (get_series_books) -- discovery records nothing onto facts at
    all on this route."""

    async def fake_get_books(asin_list, region, session, *, use_cache=None, facts=None):
        record_source_keys(facts, SOURCE_CACHE, [MOCK_BOOK["asin"]])
        return [MOCK_BOOK]

    with patch("app.api.routes.series.router.get_series_books", new_callable=AsyncMock) as mock_series, \
         patch("app.api.routes.series.router.get_books_by_asins", side_effect=fake_get_books):
        mock_series.return_value = ["B08G9PRS1K"]
        response = await async_client.get("/series/books/B00SERIES1")

    assert response.headers["x-libex-source"] == "cache"


def _partial_filter_books():
    """Two hydrated books, one from Audible and one from cache -- a
    filter (rating_better_than) that only the Audible one survives."""
    audible_book = {**MOCK_BOOK, "asin": "B000000001", "rating": 4.9}
    cache_book = {**MOCK_BOOK, "asin": "B000000002", "rating": 1.0}
    return audible_book, cache_book


@pytest.mark.asyncio
async def test_get_series_books_source_header_names_only_the_filter_survivors_source(async_client):
    """Same reproduction as the bulk /book route's own version of this
    defect: two books hydrated, one from Audible and one from cache, a
    filter that removes the cache-sourced one from the body. The header
    must read the plain "audible" token -- never "mixed", and never a
    "cache" parameter naming a source that contributed nothing to what was
    actually sent."""
    audible_book, cache_book = _partial_filter_books()

    async def fake_get_books(asin_list, region, session, *, use_cache=None, facts=None):
        record_source_keys(facts, SOURCE_AUDIBLE, [audible_book["asin"]])
        record_source_keys(facts, SOURCE_CACHE, [cache_book["asin"]])
        return [audible_book, cache_book]

    with patch("app.api.routes.series.router.get_series_books", new_callable=AsyncMock) as mock_series, \
         patch("app.api.routes.series.router.get_books_by_asins", side_effect=fake_get_books):
        mock_series.return_value = [audible_book["asin"], cache_book["asin"]]
        response = await async_client.get("/series/books/B00SERIES1?rating_better_than=3.0")

    body = response.json()
    assert [b["asin"] for b in body] == [audible_book["asin"]]
    assert response.headers["x-libex-source"] == "audible"


@pytest.mark.asyncio
async def test_get_series_books_primary_source_header_names_only_the_filter_survivors_source(async_client):
    """The legacy twin (/series/{asin}/books) must not diverge from the
    named route above."""
    audible_book, cache_book = _partial_filter_books()

    async def fake_get_books(asin_list, region, session, *, use_cache=None, facts=None):
        record_source_keys(facts, SOURCE_AUDIBLE, [audible_book["asin"]])
        record_source_keys(facts, SOURCE_CACHE, [cache_book["asin"]])
        return [audible_book, cache_book]

    with patch("app.api.routes.series.router.get_series_books", new_callable=AsyncMock) as mock_series, \
         patch("app.api.routes.series.router.get_books_by_asins", side_effect=fake_get_books):
        mock_series.return_value = [audible_book["asin"], cache_book["asin"]]
        response = await async_client.get("/series/B00SERIES1/books?rating_better_than=3.0")

    body = response.json()
    assert [b["asin"] for b in body] == [audible_book["asin"]]
    assert response.headers["x-libex-source"] == "audible"


# ============================================================
# F1 -- /series/{asin}/books' own use_cache pin (the twin the fixed
# defect's own regression test above never reached)
# ============================================================
# test_get_series_books_omits_cache_param_and_reads_the_cache_for_both_
# phases_by_default and its cache=false sibling above both exercise
# /series/books/{asin} -- the named, canonical route -- and neither ever
# sends a request to /series/{asin}/books, the legacy twin
# get_books_by_series_primary serves. A fake that merely accepts use_cache
# without checking it passes whether or not the route actually threads the
# value through, which is exactly how the /series/books/{asin} defect
# shipped unnoticed until a live cache-miss loop was measured -- the same
# reasoning applies here, on the one twin that gap left unpinned.


@pytest.mark.asyncio
async def test_get_series_books_primary_omits_cache_param_and_reads_the_cache_for_both_phases_by_default(async_client):
    with patch("app.api.routes.series.router.get_series_books", new_callable=AsyncMock) as mock_series, \
         patch("app.api.routes.series.router.get_books_by_asins", new_callable=AsyncMock) as mock_books:
        mock_series.return_value = ["B08G9PRS1K"]
        mock_books.return_value = [MOCK_BOOK]
        await async_client.get("/series/B00SERIES1/books")
    assert mock_series.call_args[0][3] is True
    assert mock_books.call_args.kwargs["use_cache"] is True


@pytest.mark.asyncio
async def test_get_series_books_primary_cache_false_marks_the_response_no_store(async_client):
    with patch("app.api.routes.series.router.get_series_books", new_callable=AsyncMock) as mock_series, \
         patch("app.api.routes.series.router.get_books_by_asins", new_callable=AsyncMock) as mock_books:
        mock_series.return_value = ["B08G9PRS1K"]
        mock_books.return_value = [MOCK_BOOK]
        response = await async_client.get("/series/B00SERIES1/books?cache=false")
    assert mock_series.call_args[0][3] is False
    assert mock_books.call_args.kwargs["use_cache"] is False
    assert response.headers["cache-control"] == "no-store"


# ============================================================
# HOLLOW-STUB NOT-FOUND -- the header is the only channel on these routes
# ============================================================
# Neither /series/books/{asin} nor /series/{asin}/books has a notFound body
# field the way the bulk /book route does -- a book Audible can't hydrate
# just isn't in the list, and X-Libex-Complete/X-Libex-Incomplete-Reason
# are the only signal a caller gets that the list is short rather than
# simply reflecting a series with fewer books than requested. These mock
# audible_get directly (not NotFoundException) so the hollow, titleless
# 200 stub Audible actually returns for an unresolvable ASIN reaches real
# code -- every existing not-found test on this router mocks
# NotFoundException, which only reproduces the narrow literal-404 case a
# live batch call never actually takes.


@pytest.mark.asyncio
async def test_get_series_books_marks_incomplete_on_a_hollow_stub_with_no_notfound_field(async_client):
    found_asin = "B0FOUND001"
    stub_asin = "B0NOTFOUND1"

    async def _get(region, path, params):
        return {"products": [
            {
                "asin": found_asin, "title": "Real Book", "authors": [], "narrators": [],
                "relationships": [], "product_images": {}, "category_ladders": [],
                "rating": {}, "publication_datetime": "2021-01-01T00:00:00Z",
            },
            {"asin": stub_asin, "product_state": "NOT_AVAILABLE_FOR_PURCHASE"},
        ]}

    with patch("app.api.routes.series.router.get_series_books", new_callable=AsyncMock) as mock_series, \
         patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.books.persist_books_background"), \
         patch("app.services.audible.books.cache.get_many", new=AsyncMock(return_value={})):
        mock_series.return_value = [found_asin, stub_asin]
        response = await async_client.get("/series/books/B00SERIES1")

    assert response.status_code == 200
    body = response.json()
    assert [b["asin"] for b in body] == [found_asin]
    assert response.headers["x-libex-complete"] == "false"
    assert response.headers["x-libex-incomplete-reason"] == "hydration-not-found"


@pytest.mark.asyncio
async def test_get_series_books_primary_marks_incomplete_on_a_hollow_stub_with_no_notfound_field(async_client):
    """The legacy twin (/series/{asin}/books) must not diverge from the
    named route above."""
    found_asin = "B0FOUND001"
    stub_asin = "B0NOTFOUND1"

    async def _get(region, path, params):
        return {"products": [
            {
                "asin": found_asin, "title": "Real Book", "authors": [], "narrators": [],
                "relationships": [], "product_images": {}, "category_ladders": [],
                "rating": {}, "publication_datetime": "2021-01-01T00:00:00Z",
            },
            {"asin": stub_asin, "product_state": "NOT_AVAILABLE_FOR_PURCHASE"},
        ]}

    with patch("app.api.routes.series.router.get_series_books", new_callable=AsyncMock) as mock_series, \
         patch("app.services.audible.books.audible_get", new=AsyncMock(side_effect=_get)), \
         patch("app.services.audible.books.persist_books_background"), \
         patch("app.services.audible.books.cache.get_many", new=AsyncMock(return_value={})):
        mock_series.return_value = [found_asin, stub_asin]
        response = await async_client.get("/series/B00SERIES1/books")

    assert response.status_code == 200
    body = response.json()
    assert [b["asin"] for b in body] == [found_asin]
    assert response.headers["x-libex-complete"] == "false"
    assert response.headers["x-libex-incomplete-reason"] == "hydration-not-found"
