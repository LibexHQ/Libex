"""
GET /db/delisted: the paginated list of hand-entered books. The reader is mocked
at the router's import path; the query itself is covered in
tests/services/db/test_delisted_reader.py (SQLite) and
tests/integration/test_manual_entry_endpoint.py (Postgres).
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from httpx import ASGITransport, AsyncClient

# Local
from app.main import app
from libex_core.audible.client import VALID_REGIONS

READER = "app.api.routes.db.router.get_delisted_books_from_db"
BOOK = {"asin": "B0MANUAL01", "title": "Delisted", "region": "us", "manualEntry": True}


@pytest.fixture
async def async_client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_the_list_is_the_readers_books_with_manual_entry_true(async_client):
    with patch(READER, new_callable=AsyncMock, return_value=[BOOK, {**BOOK, "asin": "B0MANUAL02"}]) as mock:
        response = await async_client.get("/db/delisted")
    assert response.status_code == 200
    assert [b["asin"] for b in response.json()] == ["B0MANUAL01", "B0MANUAL02"]
    assert all(b["manualEntry"] is True for b in response.json())
    mock.assert_awaited_once()


async def test_an_empty_list_is_a_200_with_an_empty_list_not_a_404(async_client):
    with patch(READER, new_callable=AsyncMock, return_value=[]):
        response = await async_client.get("/db/delisted")
    assert response.status_code == 200
    assert response.json() == []


async def test_the_defaults_are_twenty_a_page_first_page_no_region_no_sort(async_client):
    with patch(READER, new_callable=AsyncMock, return_value=[BOOK]) as mock:
        await async_client.get("/db/delisted")
    kwargs = mock.call_args.kwargs
    assert (kwargs["limit"], kwargs["page"], kwargs["region"], kwargs["sort"]) == (20, 1, None, None)
    assert kwargs["order"] == "desc"


async def test_paging_sort_order_and_region_are_forwarded(async_client):
    with patch(READER, new_callable=AsyncMock, return_value=[BOOK]) as mock:
        await async_client.get("/db/delisted?limit=5&page=3&sort=title&order=asc&region=uk")
    kwargs = mock.call_args.kwargs
    assert (kwargs["limit"], kwargs["page"], kwargs["sort"], kwargs["order"], kwargs["region"]) == (
        5, 3, "title", "asc", "uk")


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "limit=-1", "limit=x", "page=0", "page=-1",
                                   "page=x", "sort=nope", "order=sideways"])
async def test_a_bad_paging_or_sort_value_is_refused_before_the_reader(async_client, query):
    with patch(READER, new_callable=AsyncMock, return_value=[BOOK]) as mock:
        response = await async_client.get(f"/db/delisted?{query}")
    assert response.status_code == 422
    mock.assert_not_called()


async def test_the_page_limit_of_one_hundred_is_allowed(async_client):
    with patch(READER, new_callable=AsyncMock, return_value=[BOOK]) as mock:
        response = await async_client.get("/db/delisted?limit=100")
    assert response.status_code == 200 and mock.call_args.kwargs["limit"] == 100


async def test_a_bad_region_is_a_400_and_the_reader_is_not_asked(async_client):
    with patch(READER, new_callable=AsyncMock, return_value=[BOOK]) as mock:
        response = await async_client.get("/db/delisted?region=xx")
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid region: xx", "status_code": 400, "code": "invalid_request"}
    mock.assert_not_called()


@pytest.mark.parametrize("region", sorted(VALID_REGIONS))
async def test_every_region_is_accepted_and_normalised(async_client, region):
    with patch(READER, new_callable=AsyncMock, return_value=[BOOK]) as mock:
        response = await async_client.get(f"/db/delisted?region={region.upper()}")
    assert response.status_code == 200
    assert mock.call_args.kwargs["region"] == region


async def test_the_route_is_documented_with_the_error_responses_and_the_book_list(async_client):
    schema = (await async_client.get("/openapi.json")).json()
    operation = schema["paths"]["/db/delisted"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["items"] == {
        "$ref": "#/components/schemas/BookResponse"}
    assert set(operation["responses"]) == set(schema["paths"]["/db/vvab"]["get"]["responses"])
    assert "after Audible lists it again" in operation["description"]
