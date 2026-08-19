"""
Authors endpoint tests.
Tests route structure, parameter validation, and error handling.
Audible API calls are mocked — we test our code not Audible's.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import re
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient, ASGITransport

# Local
from app.main import app
from app.core.exceptions import NotFoundException
from app.services.audible.authors import AuthorBooksResult

MOCK_AUTHOR = {
    "id": None,
    "asin": "B000APF21M",
    "name": "Frank Herbert",
    "description": "Frank Herbert was an American science fiction author.",
    "image": "https://example.com/frank-herbert.jpg",
    "region": "us",
    "regions": ["us"],
    "genres": [],
    "updatedAt": "2024-01-01T00:00:00+00:00",
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
        for field in ["asin", "name", "region", "regions", "genres"]:
            assert field in data, f"Missing required field: {field}"


@pytest.mark.asyncio
async def test_get_author_returns_regions_list(async_client):
    """Author endpoint returns regions as a list."""
    with patch("app.api.routes.authors.router.get_author", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_AUTHOR
        response = await async_client.get("/author/B000APF21M")
        assert isinstance(response.json()["regions"], list)


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
        mock.return_value = {**MOCK_AUTHOR, "region": "uk", "regions": ["uk"]}
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
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], True)
        mock_asins.return_value = [MOCK_BOOK]
        response = await async_client.get("/author/books/B000APF21M")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_author_books_returns_list_of_books(async_client):
    """Author books endpoint returns a list of full book objects."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], True)
        mock_asins.return_value = [MOCK_BOOK]
        response = await async_client.get("/author/books/B000APF21M")
        data = response.json()
        assert isinstance(data, list)
        assert data[0]["asin"] == "B08G9PRS1K"


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
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], True)
        mock_asins.return_value = [MOCK_BOOK]
        await async_client.get("/author/books/B000APF21M")
        assert mock_books.call_args[0][1] == "us"


@pytest.mark.asyncio
async def test_get_author_books_hydration_passes_cache_flag_and_high_concurrency(async_client):
    """The hydration call for /author/books/{asin} must carry both
    use_cache=cache (the same flag discovery was just given, kept a single
    decision across both halves of the request) and high_concurrency=True
    (traced to a live production outage -- see the router's own comment).
    Pinning both together is deliberate: either one reverting alone, while
    the other stays fixed, is exactly how the original half-wired-flag
    defect came to exist, and it would leave the suite green if only one
    of the two were asserted."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], True)
        mock_asins.return_value = [MOCK_BOOK]
        response = await async_client.get("/author/books/B000APF21M?cache=true")

    assert response.status_code == 200
    mock_asins.assert_awaited_once()
    call_args, call_kwargs = mock_asins.call_args
    assert call_args[0] == ["B08G9PRS1K"]
    assert call_kwargs["use_cache"] is True
    assert call_kwargs["high_concurrency"] is True


@pytest.mark.asyncio
async def test_get_author_books_hydration_cache_flag_follows_query_param_false(async_client):
    """Complement to the above: use_cache must track cache=false too, not
    just be pinned True by coincidence of the other test's fixture."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], True)
        mock_asins.return_value = [MOCK_BOOK]
        await async_client.get("/author/books/B000APF21M?cache=false")

    call_kwargs = mock_asins.call_args.kwargs
    assert call_kwargs["use_cache"] is False
    assert call_kwargs["high_concurrency"] is True


@pytest.mark.asyncio
async def test_get_author_books_legacy_route_hydration_passes_cache_flag_and_high_concurrency(async_client):
    """The legacy twin route (/author/{asin}/books) must pass the same two
    args to the same call -- this is the exact route the fix's own comment
    calls out as needing the identical pairing, and the one that
    nothing else pins at all."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], True)
        mock_asins.return_value = [MOCK_BOOK]
        response = await async_client.get("/author/B000APF21M/books?cache=true")

    assert response.status_code == 200
    call_kwargs = mock_asins.call_args.kwargs
    assert call_kwargs["use_cache"] is True
    assert call_kwargs["high_concurrency"] is True


@pytest.mark.asyncio
async def test_get_author_books_defaults_to_serving_cache_on_both_phases(async_client):
    """A request that names no cache param at all must reach BOTH phases
    with use_cache=True.

    This is the property the endpoint exists in its current form to have,
    and until it was pinned nothing tested it: the two tests above pass
    ?cache=true and ?cache=false explicitly, so both stayed green across a
    change to the default in either direction. The default is the only value
    the public path ever actually uses -- callers do not pass the flag -- so
    an unpinned default meant the one code path everybody takes was the one
    path no test covered. Flipping it back to False would restore the state
    where no repeat request is ever cheap and prolific authors 504."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], True)
        mock_asins.return_value = [MOCK_BOOK]
        response = await async_client.get("/author/books/B000APF21M")

    assert response.status_code == 200
    # Discovery: positional, matching how the router calls it.
    assert mock_books.call_args[0][3] is True
    # Hydration: the other half of the same request.
    assert mock_asins.call_args.kwargs["use_cache"] is True


@pytest.mark.asyncio
async def test_get_author_books_legacy_route_defaults_to_serving_cache_too(async_client):
    """The legacy twin must default the same way. The two routes disagreeing
    on this would send identical requests down the cached path or the full
    walk depending only on which URL a caller happened to use."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], True)
        mock_asins.return_value = [MOCK_BOOK]
        response = await async_client.get("/author/B000APF21M/books")

    assert response.status_code == 200
    assert mock_books.call_args[0][3] is True
    assert mock_asins.call_args.kwargs["use_cache"] is True


@pytest.mark.asyncio
async def test_the_author_profile_route_still_defaults_to_no_cache(async_client):
    """The flipped default is scoped to the books walk, not to every route
    that happens to take a cache flag. /author/{asin} fetches one author
    profile in one request -- it is not the hundreds-of-requests walk the
    flip exists to stop paying for -- so its default was deliberately left
    alone. Pinned so that "flip the cache default" is not later applied to
    this route as a consistency tidy-up without that being an actual
    decision."""
    with patch("app.api.routes.authors.router.get_author", new_callable=AsyncMock) as mock_author:
        mock_author.return_value = MOCK_AUTHOR
        response = await async_client.get("/author/B000APF21M")

    assert response.status_code == 200
    assert mock_author.call_args[0][3] is False


@pytest.mark.asyncio
async def test_both_phases_share_one_deadline(async_client):
    """Discovery and hydration must be bounded by the SAME deadline object,
    not one each.

    This is the whole point of the change: two budgets that each start when
    their phase does add up, so the request can run to the sum of them on a
    gateway that gave up long before. Recomputing the clock for the second
    phase is the defect, and it is invisible to every other test -- a
    fresh-deadline mutation here passes the full suite, because nothing else
    looks at the value at all."""
    seen = {}

    async def _discovery(asin, region, session, cache, deadline=None):
        seen["discovery"] = deadline
        return AuthorBooksResult(["B08G9PRS1K"], True, None)

    async def _hydration(asins, region, session, **kwargs):
        seen["hydration"] = kwargs.get("deadline")
        return [MOCK_BOOK]

    with patch("app.api.routes.authors.router.get_author_books", new=_discovery), \
         patch("app.api.routes.authors.router.get_books_by_asins", new=_hydration):
        response = await async_client.get("/author/books/B000APF21M")

    assert response.status_code == 200
    assert seen["discovery"] is not None, "discovery was not given a deadline"
    assert seen["hydration"] is not None, "hydration was not given a deadline"
    assert seen["hydration"] == seen["discovery"], (
        "the two phases were given different deadlines, so they can add up"
    )


@pytest.mark.asyncio
async def test_a_freshly_walked_complete_result_gets_only_a_short_edge_ttl(async_client):
    """A walk taken just now has no trustworthy remaining life to advertise:
    its cache entry is written by a background task that has not necessarily
    committed and can fail outright. Advertising a long TTL here would let
    the edge -- and, with Tiered Cache, several upper tiers -- hold a copy
    Libex may never have persisted."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], True, None)
        mock_asins.return_value = [MOCK_BOOK]
        response = await async_client.get("/author/books/B000APF21M")

    assert response.status_code == 200
    assert response.headers["X-Libex-Complete"] == "true"
    assert "s-maxage=300" in response.headers["Cache-Control"]
    assert response.headers["Cache-Control"].startswith("public,")


@pytest.mark.asyncio
async def test_a_cached_complete_result_expires_at_the_edge_when_libex_does(async_client):
    """The point of carrying the entry's expiry out to the route. A fixed
    edge TTL drifts past Libex's own entry and the two disagree about how
    old the answer is; quoting the remaining life makes them expire
    together."""
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=4000)
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], True, expires_at)
        mock_asins.return_value = [MOCK_BOOK]
        response = await async_client.get("/author/books/B000APF21M")

    s_maxage = int(re.search(r"s-maxage=(\d+)", response.headers["Cache-Control"]).group(1))
    assert 3990 <= s_maxage <= 4000
    # Not the fresh-walk figure -- that would mean the expiry never arrived.
    assert s_maxage != 300


@pytest.mark.asyncio
async def test_an_entry_on_the_edge_of_expiry_never_advertises_a_negative_ttl(async_client):
    """An entry read as live can lapse between the read and this
    arithmetic. A negative s-maxage is nonsense to send, and floats would be
    rejected outright by some caches."""
    expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], True, expires_at)
        mock_asins.return_value = [MOCK_BOOK]
        response = await async_client.get("/author/books/B000APF21M")

    assert "s-maxage=0" in response.headers["Cache-Control"]


@pytest.mark.asyncio
async def test_an_explicitly_uncached_request_is_never_made_cacheable(async_client):
    """?cache=false must reach the Cache-Control layer, not just discovery
    and hydration.

    A caller who asks for an uncached answer and receives one marked
    `public` gets that same answer back from their own browser or the CDN on
    the next identical request -- the edge keys on the query string, so
    ?cache=false becomes its own cached object and the request stops
    reaching Libex at all for the length of the TTL. The escape hatch would
    hold inside Libex and fail one layer up, which is worse than not
    offering it."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], True, None)
        mock_asins.return_value = [MOCK_BOOK]
        response = await async_client.get("/author/books/B000APF21M?cache=false")

    assert response.status_code == 200
    assert response.headers["X-Libex-Complete"] == "true"
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_a_short_hydration_is_marked_incomplete_even_when_discovery_was_whole(async_client):
    """The header describes the BODY, not just the ASIN walk.

    Discovery can hit a complete cached list while hydration loses chunks --
    get_books_by_asins returns fewer books on a transient chunk failure only
    partly recovered from the DB, on the outage fallback, and for ASINs
    Audible no longer knows. Marking on discovery alone advertised a
    half-hydrated body as complete, and once these responses became
    cacheable that let an edge hold it for the entry's whole remaining life
    with no way to invalidate it."""
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=4000)
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B0AAA", "B0BBB", "B0CCC"], True, expires_at)
        mock_asins.return_value = [MOCK_BOOK]          # one book for three ASINs
        response = await async_client.get("/author/books/B000APF21M")

    assert response.status_code == 200
    assert response.headers["X-Libex-Complete"] == "false"
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_a_whole_hydration_of_a_whole_walk_stays_complete(async_client):
    """Complement to the above -- the shortfall check must not mark every
    response incomplete and quietly disable edge caching altogether."""
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=4000)
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B0AAA", "B0BBB"], True, expires_at)
        mock_asins.return_value = [MOCK_BOOK, MOCK_BOOK]
        response = await async_client.get("/author/books/B000APF21M")

    assert response.headers["X-Libex-Complete"] == "true"
    assert "s-maxage=" in response.headers["Cache-Control"]


@pytest.mark.asyncio
async def test_a_truncated_walk_is_marked_incomplete_and_refused_to_caches(async_client):
    """The property this endpoint most needs to have. A walk that ran out of
    time still returns 200 with the partial list -- the caller gets what
    there is -- but it is labelled, and it carries no-store so an edge cache
    cannot hold a partial and serve it to everyone for a full TTL. The
    status stays 200 deliberately; 206 was rejected because HTTP reserves it
    for range responses and CDNs route it down their partial-content
    path."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], False)
        mock_asins.return_value = [MOCK_BOOK]
        response = await async_client.get("/author/books/B000APF21M")

    assert response.status_code == 200
    assert response.headers["X-Libex-Complete"] == "false"
    # Exactly no-store, not no-store alongside a max-age: a positive
    # directive leaking onto an incomplete response is the whole failure
    # this guards, and a substring check would not catch it.
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() != []


@pytest.mark.asyncio
async def test_the_legacy_route_marks_a_truncated_walk_the_same_way(async_client):
    """The twin must not be the quiet way to get an unlabelled partial."""
    with patch("app.api.routes.authors.router.get_author_books", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], False)
        mock_asins.return_value = [MOCK_BOOK]
        response = await async_client.get("/author/B000APF21M/books")

    assert response.headers["X-Libex-Complete"] == "false"
    assert response.headers["Cache-Control"] == "no-store"


# ============================================================
# GET AUTHOR BOOKS BY NAME
# ============================================================

@pytest.mark.asyncio
async def test_get_author_books_by_name_returns_200(async_client):
    """Author books by name endpoint returns 200."""
    with patch("app.api.routes.authors.router.get_author_books_by_name", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], True)
        mock_asins.return_value = [MOCK_BOOK]
        response = await async_client.get("/author/books?name=Frank+Herbert")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_author_books_by_name_requires_name(async_client):
    """Author books by name endpoint requires name parameter."""
    response = await async_client.get("/author/books")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_author_books_by_name_returns_list(async_client):
    """Author books by name endpoint returns a list of full book objects."""
    with patch("app.api.routes.authors.router.get_author_books_by_name", new_callable=AsyncMock) as mock_books, \
         patch("app.api.routes.authors.router.get_books_by_asins", new_callable=AsyncMock) as mock_asins:
        mock_books.return_value = AuthorBooksResult(["B08G9PRS1K"], True)
        mock_asins.return_value = [MOCK_BOOK]
        response = await async_client.get("/author/books?name=Frank+Herbert")
        assert isinstance(response.json(), list)


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
        response = await async_client.get("/author?name=Frank+Herbert")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_search_authors_requires_name(async_client):
    """Author search endpoint requires name parameter."""
    response = await async_client.get("/author")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_search_authors_returns_list(async_client):
    """Author search endpoint returns a list."""
    with patch("app.api.routes.authors.router.search_authors", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_AUTHOR]
        response = await async_client.get("/author?name=Frank+Herbert")
        assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_search_authors_returns_404_when_none_found(async_client):
    """Author search returns 404 when no authors found."""
    with patch("app.api.routes.authors.router.search_authors", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/author?name=NotAnAuthor")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_search_authors_all_regions(async_client):
    """Author search works for all supported regions."""
    regions = ["us", "uk", "ca", "au", "de", "fr", "it", "es", "jp", "in", "br"]
    with patch("app.api.routes.authors.router.search_authors", new_callable=AsyncMock) as mock:
        mock.return_value = [MOCK_AUTHOR]
        for region in regions:
            response = await async_client.get(f"/author?name=Frank+Herbert&region={region}")
            assert response.status_code == 200, f"Failed for region: {region}"


@pytest.mark.asyncio
async def test_get_author_rejects_invalid_asin(async_client):
    """Author endpoint rejects malformed ASIN."""
    response = await async_client.get("/author/not-an-asin")
    assert response.status_code == 404
    assert "Invalid ASIN" in response.json()["error"]


@pytest.mark.asyncio
async def test_get_author_books_rejects_invalid_asin(async_client):
    """Author books endpoint rejects malformed ASIN."""
    response = await async_client.get("/author/books/not-an-asin")
    assert response.status_code == 404
    assert "Invalid ASIN" in response.json()["error"]