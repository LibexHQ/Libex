"""
Region on /db/plans and /db/genres.

Neither list is region-scoped, but a region that is given is still validated
the same way every other region-taking route does: `xx` is a 400, any case of
a real region is accepted, and leaving it out changes nothing.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from httpx import ASGITransport, AsyncClient

# Local
from app.main import app

_ROUTES = [
    ("/db/plans", "get_distinct_plans_from_db"),
    ("/db/genres", "get_distinct_genres_from_db"),
]


@pytest.fixture
async def async_client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _get(client, url, reader, query):
    with patch(f"app.api.routes.db.router.{reader}", new_callable=AsyncMock) as mock:
        mock.return_value = ["a"]
        response = await client.get(url + query)
    return response, mock


@pytest.mark.asyncio
@pytest.mark.parametrize("url,reader", _ROUTES)
async def test_invalid_region_is_a_400(async_client, url, reader):
    response, mock = await _get(async_client, url, reader, "?region=xx")
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid region: xx", "status_code": 400, "code": "invalid_request"}
    mock.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("url,reader", _ROUTES)
async def test_uppercase_region_matches_lowercase(async_client, url, reader):
    upper, _ = await _get(async_client, url, reader, "?region=US")
    lower, _ = await _get(async_client, url, reader, "?region=us")
    assert upper.status_code == lower.status_code == 200
    assert upper.json() == lower.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("url,reader", _ROUTES)
async def test_omitted_region_is_unchanged(async_client, url, reader):
    response, mock = await _get(async_client, url, reader, "")
    assert response.status_code == 200
    assert response.json() == ["a"]
    mock.assert_called_once()
