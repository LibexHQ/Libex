"""
A non-JSON 200 from Audible, through the real hosted client and a real route,
answers exactly as any other Audible failure always has: the stored copy when
there is one, otherwise the 503 outage shape. Only the transport is replaced.
"""

# Standard library
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import httpx
import pytest
from httpx import ASGITransport, AsyncClient

# Local
from app.db.session import get_session
from app.main import app
from app.services.audible import _hosted_client
from tests.fixtures.outage import assert_outage_503

SERIES_ASIN = "B0SERIES1X"
STORED = {"asin": SERIES_ASIN, "name": "A Series", "region": "us"}


@pytest.fixture
async def client():
    app.dependency_overrides[get_session] = lambda: MagicMock()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


def _html_200_transport():
    """Replaces the hosted client's own httpx client, not httpx.AsyncClient.get,
    which the test client below also uses."""
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"<html>Please verify you are human</html>")
        )
    )
    return patch.object(_hosted_client, "_get_client", return_value=upstream)


async def test_non_json_200_serves_the_stored_series_as_any_outage_does(client):
    with (
        _html_200_transport(),
        patch("app.services.audible.series.get_series_from_db", AsyncMock(return_value=STORED)),
        patch("app.services.audible.series.cache.get", AsyncMock(return_value=None)),
    ):
        response = await client.get(f"/series/{SERIES_ASIN}")
    assert response.status_code == 200
    assert response.json()["asin"] == SERIES_ASIN


async def test_non_json_200_without_a_stored_copy_is_the_503_outage(client):
    with (
        _html_200_transport(),
        patch("app.services.audible.series.get_series_from_db", AsyncMock(return_value=None)),
        patch("app.services.audible.series.cache.get", AsyncMock(return_value=None)),
    ):
        response = await client.get(f"/series/{SERIES_ASIN}")
    assert_outage_503(response, "Audible unavailable and no cached series data found")
