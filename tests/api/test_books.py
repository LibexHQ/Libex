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
from app.api.routes.large_response import LARGE_RESPONSE_THREAD_THRESHOLD
from app.core.response_headers import (
    REASON_HYDRATION_FAILED,
    SOURCE_AUDIBLE,
    SOURCE_CACHE,
    SOURCE_DB,
    record_incomplete,
    record_source,
    record_source_keys,
)


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
async def test_bulk_books_near_the_1000_asin_cap_returns_every_book(async_client):
    """The bulk endpoint's own maximum is large enough to cross
    LARGE_RESPONSE_THREAD_THRESHOLD -- this is the offload path from
    app.api.routes.large_response, not the inline one, and it must still
    return every book found plus an accurate notFound list."""
    n = 999
    assert n + 1 >= LARGE_RESPONSE_THREAD_THRESHOLD
    books = [{**MOCK_BOOK, "asin": f"B{i:09d}"} for i in range(n)]
    found_asins = [b["asin"] for b in books]
    missing_asin = "B999999999"  # brings the request to exactly the 1000-ASIN cap
    with patch("app.api.routes.books.router.get_books_by_asins", new_callable=AsyncMock) as mock:
        mock.return_value = books
        response = await async_client.get(
            f"/book?asins={','.join(found_asins)},{missing_asin}"
        )

    assert response.status_code == 200
    data = response.json()
    assert len(data["books"]) == n
    assert {b["asin"] for b in data["books"]} == set(found_asins)
    assert data["notFound"] == [missing_asin]


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

# ============================================================
# CACHE DEFAULT FLIP -- omitting cache now reads the cache
# ============================================================
# /book/{asin} and /book (bulk) are two of the Standard routes flipped from
# cache=False to cache=True. See cache_param.CacheStandardParam for why.


@pytest.mark.asyncio
async def test_get_book_omits_cache_param_and_reads_the_cache_by_default(async_client):
    with patch("app.api.routes.books.router.get_book_by_asin", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_BOOK
        await async_client.get("/book/B08G9PRS1K")
    assert mock.call_args[0][3] is True


@pytest.mark.asyncio
async def test_get_book_cache_false_skips_the_read_and_marks_the_response_no_store(async_client):
    with patch("app.api.routes.books.router.get_book_by_asin", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_BOOK
        response = await async_client.get("/book/B08G9PRS1K?cache=false")
    assert mock.call_args[0][3] is False
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_get_book_cache_true_sends_no_cache_control_header(async_client):
    """apply_cache_control never sets a positive Cache-Control -- a
    cache=true response (or the omitted-param default, same value) carries
    none at all, matching the eight of ten routes that sent no
    Cache-Control before this parameter existed."""
    with patch("app.api.routes.books.router.get_book_by_asin", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_BOOK
        response = await async_client.get("/book/B08G9PRS1K?cache=true")
    assert "cache-control" not in response.headers


@pytest.mark.asyncio
async def test_get_books_bulk_omits_cache_param_and_reads_the_cache_by_default(async_client):
    with patch("app.api.routes.books.router.get_books_by_asins", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        await async_client.get(f"/book?asins={MOCK_BOOK['asin']}")
    assert mock.call_args[0][3] is True


@pytest.mark.asyncio
async def test_get_books_bulk_cache_false_marks_the_response_no_store(async_client):
    with patch("app.api.routes.books.router.get_books_by_asins", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_BOOK]
        response = await async_client.get(f"/book?asins={MOCK_BOOK['asin']}&cache=false")
    assert mock.call_args[0][3] is False
    assert response.headers["cache-control"] == "no-store"


# ============================================================
# X-LIBEX-SOURCE / X-LIBEX-COMPLETE -- stamped from the router's facts ledger
# ============================================================
# These mock the service one call above the router (get_book_by_asin /
# get_books_by_asins), with a side_effect that records onto the real
# ResponseFacts the router constructs and passes in -- so what's under test
# is the router's own stamping logic (stamp_facts_headers, which every
# endpoint in this router calls), not which source get_book_by_asin/
# get_books_by_asins itself credits for a given ASIN. That per-key
# attribution -- including the mixed-source and DB-backstop/outage-fallback
# cases a router-level mock can't reach -- is covered by the
# "RESPONSE FACTS -- SOURCE ATTRIBUTION" section of
# tests/services/test_books_service.py.


@pytest.mark.asyncio
async def test_get_book_source_header_reflects_the_single_recorded_source(async_client):
    async def fake_get_book(asin, region, session, cache, *, facts=None):
        record_source(facts, SOURCE_CACHE)
        return MOCK_BOOK

    with patch("app.api.routes.books.router.get_book_by_asin", side_effect=fake_get_book):
        response = await async_client.get("/book/B08G9PRS1K")

    assert response.headers["x-libex-source"] == "cache"
    assert response.headers["x-libex-complete"] == "true"


@pytest.mark.asyncio
async def test_get_book_source_header_false_on_a_real_hydration_shortfall(async_client):
    """Through the real stack rather than a router-level fake facts
    recorder: Audible down, the pre-fetch cache empty, and the DB backstop
    the only thing that answers. What has to reach this response is
    get_books_by_asins' own outage DB-fallback path (see books.py) running
    for real, not a stand-in for it.

    X-Libex-Complete now asserts element coverage, not "did some internal
    retry occur" -- and a single-ASIN request's DB fallback recovering the
    one ASIN it was asked for is, by definition, full coverage: there is no
    way for get_book_by_asin's own request to come back with *fewer than
    all* of the one thing it asked for and still reach a 200 at all. So
    despite the outage this response is complete, not short -- proving the
    exact scenario this test's name describes is no longer classified as a
    shortfall now that the header tracks coverage rather than "any transient
    failure happened along the way."."""
    db_book = {**MOCK_BOOK}

    with patch(
        "app.services.audible.books.audible_get",
        new=AsyncMock(side_effect=RuntimeError("Audible down")),
    ), patch(
        "app.services.audible.books.cache.get", new=AsyncMock(return_value=None)
    ), patch(
        "app.services.audible.books.get_books_from_db",
        new=AsyncMock(return_value=[db_book]),
    ):
        response = await async_client.get("/book/B08G9PRS1K")

    assert response.status_code == 200
    assert response.json()["asin"] == db_book["asin"]
    assert response.headers["x-libex-source"] == "db"
    assert response.headers["x-libex-complete"] == "true"
    assert "x-libex-incomplete-reason" not in response.headers


@pytest.mark.asyncio
async def test_get_books_bulk_source_header_false_on_a_real_hydration_shortfall(async_client):
    """The bulk endpoint's own version of the real-stack outage above, with
    two requested ASINs instead of one -- unlike the single-book route, a
    bulk request CAN come back with fewer than every requested element and
    still be a 200, so this is where a genuine coverage shortfall through
    the real stack is actually reachable. Audible down, no pre-fetch cache
    hit for either ASIN, and the DB fallback recovering only one of the
    two -- the response must be marked incomplete with the hydration-failed
    token, and the uncovered ASIN must show up in notFound, not silently
    vanish."""
    recovered_asin = MOCK_BOOK["asin"]
    missing_asin = "B000000001"
    recovered_book = {**MOCK_BOOK}

    with patch(
        "app.services.audible.books.audible_get",
        new=AsyncMock(side_effect=RuntimeError("Audible down")),
    ), patch(
        "app.services.audible.books.cache.get_many", new=AsyncMock(return_value={})
    ), patch(
        "app.services.audible.books.get_books_from_db",
        new=AsyncMock(return_value=[recovered_book]),
    ):
        response = await async_client.get(f"/book?asins={recovered_asin},{missing_asin}")

    assert response.status_code == 200
    data = response.json()
    assert [b["asin"] for b in data["books"]] == [recovered_asin]
    assert data["notFound"] == [missing_asin]
    assert response.headers["x-libex-source"] == "db"
    assert response.headers["x-libex-complete"] == "false"
    assert response.headers["x-libex-incomplete-reason"] == "hydration-failed"


@pytest.mark.asyncio
async def test_get_books_bulk_source_header_is_mixed_with_counts_summing_to_the_body(async_client):
    async def fake_get_books_by_asins(asin_list, region, session, cache, *, facts=None):
        record_source_keys(facts, SOURCE_AUDIBLE, ["B000000000", "B000000001"])
        record_source_keys(facts, SOURCE_CACHE, ["B000000002"])
        return [{**MOCK_BOOK, "asin": f"B{i:09d}"} for i in range(3)]

    with patch("app.api.routes.books.router.get_books_by_asins", side_effect=fake_get_books_by_asins):
        response = await async_client.get("/book?asins=B000000001,B000000002,B000000003")

    assert response.status_code == 200
    source_header = response.headers["x-libex-source"]
    assert source_header == "mixed; audible=2; cache=1"
    counts = [int(part.split("=")[1]) for part in source_header.split(";")[1:]]
    assert sum(counts) == len(response.json()["books"])


@pytest.mark.asyncio
async def test_get_books_bulk_can_be_both_mixed_source_and_incomplete_at_once(async_client):
    """The realistic shape of a partial bulk hydration failure under
    coverage semantics: two of three requested ASINs come back -- one live
    from Audible, one recovered from the DB backstop after a chunk failed
    transiently -- and the third is a genuine, uncovered gap. Two populated
    sources, "mixed", AND a real shortfall the caller must be told about, on
    the very same response. X-Libex-Source and X-Libex-Complete are stamped
    from the same facts object but read two independent fields on it
    (source_counts/source_by_key vs. incomplete_reasons) -- this is the one
    test that proves neither stamping step steps on the other when both are
    populated in the same request, rather than each header only ever being
    exercised alone.

    A body carrying every requested element could no longer produce
    incomplete=false at all (see the coverage-semantics rewrite of the
    single-book shortfall test above) -- so the third, missing ASIN here is
    load-bearing, not incidental set dressing."""

    async def fake_get_books_by_asins(asin_list, region, session, cache, *, facts=None):
        record_source_keys(facts, SOURCE_AUDIBLE, ["B000000000"])
        record_source_keys(facts, SOURCE_DB, ["B000000001"])
        record_incomplete(facts, REASON_HYDRATION_FAILED)
        return [{**MOCK_BOOK, "asin": f"B{i:09d}"} for i in range(2)]

    with patch("app.api.routes.books.router.get_books_by_asins", side_effect=fake_get_books_by_asins):
        response = await async_client.get("/book?asins=B000000000,B000000001,B000000002")

    assert response.status_code == 200
    data = response.json()
    assert data["notFound"] == ["B000000002"]
    assert response.headers["x-libex-source"] == "mixed; audible=1; db=1"
    assert response.headers["x-libex-complete"] == "false"
    assert response.headers["x-libex-incomplete-reason"] == "hydration-failed"


@pytest.mark.asyncio
async def test_get_books_bulk_source_header_omitted_when_post_filter_body_is_empty(async_client):
    """filter_dicts can empty a body after facts already recorded a source
    for what was fetched -- the header must follow the body actually sent,
    not the tally recorded before filtering ran, or a caller would see a
    source attributed to zero returned elements."""

    async def fake_get_books_by_asins(asin_list, region, session, cache, *, facts=None):
        record_source(facts, SOURCE_AUDIBLE, 1)
        return [MOCK_BOOK]  # rating 4.5

    with patch("app.api.routes.books.router.get_books_by_asins", side_effect=fake_get_books_by_asins):
        response = await async_client.get(
            f"/book?asins={MOCK_BOOK['asin']}&rating_better_than=999"
        )

    assert response.status_code == 200
    assert response.json()["books"] == []
    assert "x-libex-source" not in response.headers
    # X-Libex-Complete is unconditional and unaffected by the same emptying.
    assert response.headers["x-libex-complete"] == "true"


@pytest.mark.asyncio
async def test_get_books_bulk_source_header_names_only_the_filter_survivors_source(async_client):
    """The exact reproduction of the shipped defect: two books fetched, one
    from Audible and one from cache, a filter that removes the cache-sourced
    one. The header must read the plain "audible" token for the one
    surviving book -- never "mixed", and never a "cache" parameter naming a
    source that contributed nothing to the body actually sent."""
    audible_book = {**MOCK_BOOK, "asin": "B000000001", "rating": 4.9}
    cache_book = {**MOCK_BOOK, "asin": "B000000002", "rating": 1.0}

    async def fake_get_books_by_asins(asin_list, region, session, cache, *, facts=None):
        record_source_keys(facts, SOURCE_AUDIBLE, [audible_book["asin"]])
        record_source_keys(facts, SOURCE_CACHE, [cache_book["asin"]])
        return [audible_book, cache_book]

    with patch("app.api.routes.books.router.get_books_by_asins", side_effect=fake_get_books_by_asins):
        response = await async_client.get(
            f"/book?asins={audible_book['asin']},{cache_book['asin']}&rating_better_than=3.0"
        )

    assert response.status_code == 200
    body = response.json()["books"]
    assert [b["asin"] for b in body] == [audible_book["asin"]]
    assert response.headers["x-libex-source"] == "audible"


# ============================================================
# LARGE-RESPONSE TRAP -- headers must survive the offloaded path too
# ============================================================
# tests/api/test_large_response.py already proves the helper itself merges
# an injected response's headers. This proves the real route wiring gets a
# real header there in the first place, above the threshold, where the
# normal FastAPI header merge is bypassed in favour of the helper's own.


@pytest.mark.asyncio
async def test_bulk_books_above_threshold_still_carries_facts_and_cache_headers(async_client):
    n = LARGE_RESPONSE_THREAD_THRESHOLD
    books = [{**MOCK_BOOK, "asin": f"B{i:09d}"} for i in range(n)]
    found_asins = [b["asin"] for b in books]

    async def fake_get_books_by_asins(asin_list, region, session, cache, *, facts=None):
        record_source_keys(facts, SOURCE_AUDIBLE, found_asins)
        return books

    with patch("app.api.routes.books.router.get_books_by_asins", side_effect=fake_get_books_by_asins):
        response = await async_client.get(
            f"/book?asins={','.join(found_asins)}&cache=false"
        )

    assert response.status_code == 200
    assert len(response.json()["books"]) == n
    assert response.headers["x-libex-source"] == "audible"
    assert response.headers["x-libex-complete"] == "true"
    assert response.headers["cache-control"] == "no-store"
