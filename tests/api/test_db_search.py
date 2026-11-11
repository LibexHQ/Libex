"""
/db/search/books, /db/search/authors and /db/search/series route tests.
The ranked readers are mocked at the search router's import location: this
tests the input checks, the arguments handed on, and the status codes. The
ranking itself is tested against Postgres in tests/libex_core/storage.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from httpx import ASGITransport, AsyncClient

# Local
from app.main import app
from libex_core.exceptions import ErrorCode, LibexException
from libex_core.models import AuthorResponse, SeriesResponse, SeriesSearchResponse
from tests.api.test_db import MOCK_BOOK

BOOKS = "app.api.routes.db.search.search_books_ranked_from_db"
AUTHORS = "app.api.routes.db.search.search_authors_ranked_from_db"
SERIES = "app.api.routes.db.search.search_series_ranked_from_db"

MOCK_AUTHOR = {
    "id": 1, "asin": "B000TEST01", "name": "Test Author", "description": None, "image": None,
    "region": "us", "regions": ["us"], "genres": [], "updatedAt": None,
}
MOCK_SERIES = {
    "asin": "B0SERIES01", "name": "Test Saga", "description": None, "region": "us", "position": None,
    "updatedAt": None, "audibleExtras": None, "extrasWithheld": None, "bookCount": 3,
}

URLS = {
    "books": ("/db/search/books", "title", BOOKS),
    "authors": ("/db/search/authors", "name", AUTHORS),
    "series": ("/db/search/series", "name", SERIES),
}
MOCKS = {"books": MOCK_BOOK, "authors": MOCK_AUTHOR, "series": MOCK_SERIES}


@pytest.fixture
async def async_client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ============================================================
# ANSWERS
# ============================================================

@pytest.mark.parametrize("kind", sorted(URLS))
async def test_a_match_is_returned_as_a_list_of_records(async_client, kind):
    path, param, target = URLS[kind]
    with patch(target, new_callable=AsyncMock, return_value=[MOCKS[kind]]) as reader:
        response = await async_client.get(path, params={param: "some text"})
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list) and len(body) == 1
    assert reader.await_count == 1


async def test_a_book_search_hands_every_field_and_the_defaults_to_the_reader(async_client):
    with patch(BOOKS, new_callable=AsyncMock, return_value=[MOCK_BOOK]) as reader:
        await async_client.get(
            "/db/search/books",
            params={"title": " Dragon Quest ", "author": "Ann", "narrator": "Nina", "series": "Cycle", "language": "english"},
        )
    kwargs = reader.await_args.kwargs
    assert kwargs == {
        "title": "Dragon Quest", "author": "Ann", "narrator": "Nina", "series": "Cycle",
        "region": None, "language": "english", "limit": 10,
    }


async def test_a_book_search_with_one_field_leaves_the_others_unset(async_client):
    with patch(BOOKS, new_callable=AsyncMock, return_value=[MOCK_BOOK]) as reader:
        await async_client.get("/db/search/books", params={"author": "Ann"})
    kwargs = reader.await_args.kwargs
    assert kwargs["author"] == "Ann"
    assert kwargs["title"] is None and kwargs["narrator"] is None and kwargs["series"] is None


@pytest.mark.parametrize("kind", sorted(URLS))
async def test_the_length_limits_are_counted_after_nfkc_expands_a_character(async_client, kind):
    path, param, target = URLS[kind]
    with patch(target, new_callable=AsyncMock, return_value=[MOCKS[kind]]) as reader:
        # One ligature is one character raw, two once normalised: long enough.
        expands_to_pass = await async_client.get(path, params={param: "\ufb00"})
        # 101 ligatures are 101 raw but 202 normalised: too long.
        expands_to_fail = await async_client.get(path, params={param: "\ufb00" * 101})
        # One plain character stays too short.
        plain = await async_client.get(path, params={param: "f"})
    assert (expands_to_pass.status_code, expands_to_fail.status_code, plain.status_code) == (200, 422, 422)
    assert reader.await_count == 1
    call = reader.await_args
    assert (call.args[1] if len(call.args) > 1 else call.kwargs.get("title")) == "ff"


@pytest.mark.parametrize("kind", ["authors", "series"])
async def test_exact_and_fuzzy_are_handed_on_and_fuzzy_is_the_default(async_client, kind):
    path, param, target = URLS[kind]
    with patch(target, new_callable=AsyncMock, return_value=[MOCKS[kind]]) as reader:
        await async_client.get(path, params={param: "Brian Lee"})
        await async_client.get(path, params={param: "Brian Lee", "match": "exact"})
        await async_client.get(path, params={param: "Brian Lee", "match": "fuzzy"})
    exact_flags = [call.kwargs["exact"] for call in reader.await_args_list]
    assert exact_flags == [False, True, False]


@pytest.mark.parametrize("kind", sorted(URLS))
async def test_a_region_is_validated_and_handed_on(async_client, kind):
    path, param, target = URLS[kind]
    with patch(target, new_callable=AsyncMock, return_value=[MOCKS[kind]]) as reader:
        await async_client.get(path, params={param: "text"})
        await async_client.get(path, params={param: "text", "region": "UK"})
    assert [c.kwargs["region"] for c in reader.await_args_list] == [None, "uk"]


# ============================================================
# NO MATCH, BAD INPUT, FAILURES
# ============================================================

@pytest.mark.parametrize("kind", sorted(URLS))
async def test_no_match_is_a_404_not_in_libex(async_client, kind):
    path, param, target = URLS[kind]
    with patch(target, new_callable=AsyncMock, return_value=[]):
        response = await async_client.get(path, params={param: "nothing here"})
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == ErrorCode.NOT_IN_LIBEX.value == "not_in_libex"
    assert "No " in body["error"] and "found" in body["error"]


@pytest.mark.parametrize("kind", sorted(URLS))
async def test_an_unknown_region_is_a_400_and_nothing_is_read(async_client, kind):
    path, param, target = URLS[kind]
    with patch(target, new_callable=AsyncMock, return_value=[MOCKS[kind]]) as reader:
        response = await async_client.get(path, params={param: "text", "region": "zz"})
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid region: zz", "status_code": 400, "code": "invalid_request"}
    assert reader.await_count == 0


BAD_TEXT = {
    "one character": "a",
    "only spaces": "     ",
    "a full-width space and one letter": "　a　",
    "a full-width space only": "　　　",
    "over 200 characters": "x" * 201,
    "a NUL": "ab\x00cd",
}


@pytest.mark.parametrize("kind", sorted(URLS))
@pytest.mark.parametrize("name", sorted(BAD_TEXT))
async def test_text_that_is_too_short_too_long_or_holds_a_nul_is_a_422_and_nothing_is_read(async_client, kind, name):
    path, param, target = URLS[kind]
    with patch(target, new_callable=AsyncMock, return_value=[MOCKS[kind]]) as reader:
        response = await async_client.get(path, params={param: BAD_TEXT[name]})
    assert response.status_code == 422, response.text
    assert reader.await_count == 0


@pytest.mark.parametrize("kind", sorted(URLS))
async def test_the_length_limits_are_counted_after_the_strip_and_the_edges_pass(async_client, kind):
    path, param, target = URLS[kind]
    with patch(target, new_callable=AsyncMock, return_value=[MOCKS[kind]]) as reader:
        short = await async_client.get(path, params={param: "  ab  "})
        long = await async_client.get(path, params={param: "  " + "x" * 200 + "  "})
        full_width = await async_client.get(path, params={param: "　ab　"})
    assert (short.status_code, long.status_code, full_width.status_code) == (200, 200, 200)
    assert [c.args[1] if len(c.args) > 1 else c.kwargs.get("title") for c in reader.await_args_list] == ["ab", "x" * 200, "ab"]


@pytest.mark.parametrize("kind", ["authors", "series"])
async def test_a_match_value_that_is_neither_fuzzy_nor_exact_is_a_422(async_client, kind):
    path, param, target = URLS[kind]
    with patch(target, new_callable=AsyncMock, return_value=[MOCKS[kind]]) as reader:
        response = await async_client.get(path, params={param: "text", "match": "like"})
    assert response.status_code == 422
    assert reader.await_count == 0


@pytest.mark.parametrize("kind", sorted(URLS))
@pytest.mark.parametrize("limit,status", [(1, 200), (50, 200), (0, 422), (51, 422), (-3, 422), ("x", 422)])
async def test_limit_is_one_to_fifty(async_client, kind, limit, status):
    path, param, target = URLS[kind]
    with patch(target, new_callable=AsyncMock, return_value=[MOCKS[kind]]) as reader:
        response = await async_client.get(path, params={param: "text", "limit": limit})
    assert response.status_code == status
    assert reader.await_count == (1 if status == 200 else 0)
    if status == 200:
        assert reader.await_args.kwargs["limit"] == limit


@pytest.mark.parametrize("path", ["/db/search/books", "/db/search/authors", "/db/search/series"])
async def test_no_text_param_at_all_is_a_404_invalid_request(async_client, path):
    response = await async_client.get(path)
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "invalid_request"
    assert "No search parameters provided" in body["error"]


async def test_a_book_search_with_only_region_and_language_is_still_no_search(async_client):
    with patch(BOOKS, new_callable=AsyncMock) as reader:
        response = await async_client.get("/db/search/books", params={"region": "us", "language": "english"})
    assert response.status_code == 404 and response.json()["code"] == "invalid_request"
    assert reader.await_count == 0


@pytest.mark.parametrize("kind", sorted(URLS))
async def test_a_failed_read_is_a_503_with_a_retry_hint_not_a_404(async_client, kind):
    path, param, target = URLS[kind]
    failure = LibexException("The local database could not be read right now", status_code=503, code=ErrorCode.UPSTREAM_UNAVAILABLE)
    with patch(target, new_callable=AsyncMock, side_effect=failure):
        response = await async_client.get(path, params={param: "text"})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    assert response.json()["code"] == "upstream_unavailable"


def test_there_is_no_bare_search_route():
    paths = {route.path for route in app.routes}
    assert {"/db/search/books", "/db/search/authors", "/db/search/series"} <= paths
    assert "/db/search" not in paths and "/db/search/" not in paths


# ============================================================
# RESPONSE SHAPES
# ============================================================

def test_series_response_is_unchanged_and_the_search_response_adds_book_count_only():
    assert "bookCount" not in SeriesResponse.model_fields
    assert set(SeriesSearchResponse.model_fields) == set(SeriesResponse.model_fields) | {"bookCount"}
    assert SeriesSearchResponse.model_fields["bookCount"].default == 0
    assert issubclass(SeriesSearchResponse, SeriesResponse)


async def test_the_series_route_carries_book_count_and_the_single_lookup_model_does_not():
    assert "bookCount" in SeriesSearchResponse.model_validate(MOCK_SERIES).model_dump()
    assert "bookCount" not in SeriesResponse.model_validate({**MOCK_SERIES}).model_dump()


async def test_the_series_route_returns_book_count(async_client):
    with patch(SERIES, new_callable=AsyncMock, return_value=[MOCK_SERIES, {**MOCK_SERIES, "asin": "B0SERIES02", "bookCount": 0}]):
        response = await async_client.get("/db/search/series", params={"name": "Saga"})
    assert [(s["asin"], s["bookCount"]) for s in response.json()] == [("B0SERIES01", 3), ("B0SERIES02", 0)]


async def test_the_series_route_gives_book_count_zero_when_the_reader_omits_it(async_client):
    bare = {k: v for k, v in MOCK_SERIES.items() if k != "bookCount"}
    with patch(SERIES, new_callable=AsyncMock, return_value=[bare]):
        response = await async_client.get("/db/search/series", params={"name": "Saga"})
    assert response.json()[0]["bookCount"] == 0


async def test_the_author_route_returns_the_author_response_fields(async_client):
    with patch(AUTHORS, new_callable=AsyncMock, return_value=[MOCK_AUTHOR]):
        response = await async_client.get("/db/search/authors", params={"name": "Test Author"})
    assert set(response.json()[0]) == set(AuthorResponse.model_fields)
    assert response.json()[0]["asin"] == "B000TEST01"


async def test_the_books_route_returns_full_book_records_in_the_readers_order(async_client):
    second = {**MOCK_BOOK, "asin": "B0SECOND01"}
    with patch(BOOKS, new_callable=AsyncMock, return_value=[second, MOCK_BOOK]):
        response = await async_client.get("/db/search/books", params={"title": "Test Book"})
    assert [b["asin"] for b in response.json()] == ["B0SECOND01", MOCK_BOOK["asin"]]
    assert response.json()[1]["title"] == "Test Book"


# ============================================================
# LANGUAGE
# ============================================================

@pytest.mark.parametrize("language", ["en\x00glish", "x" * 201])
async def test_a_language_with_a_nul_or_over_200_characters_is_a_422_and_nothing_is_read(async_client, language):
    with patch(BOOKS, new_callable=AsyncMock, return_value=[MOCK_BOOK]) as reader:
        response = await async_client.get("/db/search/books", params={"title": "text", "language": language})
    assert response.status_code == 422, response.text
    assert reader.await_count == 0


async def test_a_language_of_exactly_200_characters_is_handed_on_unchanged(async_client):
    with patch(BOOKS, new_callable=AsyncMock, return_value=[MOCK_BOOK]) as reader:
        response = await async_client.get("/db/search/books", params={"title": "text", "language": "x" * 200})
    assert response.status_code == 200
    assert reader.await_args.kwargs["language"] == "x" * 200
