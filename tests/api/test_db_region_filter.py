"""
Region on the /db list routes.

The region filter reaches `Book.region == region`, which is a native Postgres
enum compared against lowercase values, so it has to be normalised and
validated in the route: `US` must match, and `xx` must be the same 400 every
other region-taking route gives, not a 404 from a swallowed database error.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from httpx import ASGITransport, AsyncClient

# Local
from app.main import app
from libex_core.audible.client import VALID_REGIONS


@pytest.fixture
async def async_client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


_BOOK = [{"asin": "B08G9PRS1K", "title": "t", "region": "us"}]

# (url, reader patched at the router's import location, reader kwarg carrying the region)
_ROUTES = [
    ("/db/book?title=x&region={r}", "search_books_from_db", "region"),
    ("/db/vvab?region={r}", "get_vvab_books_from_db", "region"),
    ("/db/author/B000APF21M/books?book_region={r}", "get_author_books_from_db", "book_region"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url,reader,kwarg", _ROUTES)
async def test_uppercase_region_is_normalised_before_the_reader(async_client, url, reader, kwarg):
    with patch(f"app.api.routes.db.router.{reader}", new_callable=AsyncMock) as mock:
        mock.return_value = _BOOK
        response = await async_client.get(url.format(r="US"))
    assert response.status_code == 200
    assert mock.call_args.kwargs[kwarg] == "us"


@pytest.mark.asyncio
@pytest.mark.parametrize("url,reader,kwarg", _ROUTES)
async def test_invalid_region_is_a_400_not_a_404(async_client, url, reader, kwarg):
    with patch(f"app.api.routes.db.router.{reader}", new_callable=AsyncMock) as mock:
        mock.return_value = _BOOK
        response = await async_client.get(url.format(r="xx"))
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid region: xx", "status_code": 400, "code": "invalid_request"}
    mock.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("region", sorted(VALID_REGIONS))
async def test_every_region_is_accepted(async_client, region):
    with patch("app.api.routes.db.router.get_vvab_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = _BOOK
        response = await async_client.get(f"/db/vvab?region={region}")
    assert response.status_code == 200
    assert mock.call_args.kwargs["region"] == region


@pytest.mark.asyncio
async def test_no_region_still_forwards_none(async_client):
    with patch("app.api.routes.db.router.get_vvab_books_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = _BOOK
        await async_client.get("/db/vvab")
    assert mock.call_args.kwargs["region"] is None
