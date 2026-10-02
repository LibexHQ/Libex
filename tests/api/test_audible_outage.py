"""
Tests for the wire seam between the service layer's two failure types and
this HTTP surface's two answers.

outage_as_unavailable re-raises the AudibleAPIException a service call raises
(optionally under the route's own message) so the exception handler in
app.main answers it as a 503 with Retry-After; a confirmed absence stays a
404. The handler tests below pin the 503 body and headers, and that nothing
else gets them.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
from app.api.routes.audible_outage import outage_as_unavailable
from libex_core.exceptions import (
    AudibleAPIException,
    CacheException,
    NotFoundException,
)
from tests.fixtures.outage import RETRY_AFTER_SECONDS, assert_outage_503

ASIN = "B08G9PRS1K"


async def _raise(exc):
    raise exc


async def _succeed(value):
    return value


@pytest.mark.asyncio
async def test_outage_as_unavailable_passes_through_a_successful_result():
    result = await outage_as_unavailable(_succeed(["B000000001"]))
    assert result == ["B000000001"]


@pytest.mark.asyncio
async def test_outage_as_unavailable_no_message_reraises_the_original_exception():
    """message left as None (the default at most call sites): the
    AudibleAPIException the service built is already the right thing for a
    caller to see, so it propagates as the very same object."""
    original = AudibleAPIException(
        "Audible unavailable and no cached data found", upstream_status=500
    )
    with pytest.raises(AudibleAPIException) as exc:
        await outage_as_unavailable(_raise(original))
    assert exc.value is original


@pytest.mark.asyncio
async def test_outage_as_unavailable_explicit_message_replaces_the_message_and_keeps_upstream_status():
    """A call site with its own literal for the failed lookup (e.g. /search's
    "No books found") gets that literal verbatim, and the real upstream
    status the failure came with survives the re-raise."""
    with pytest.raises(AudibleAPIException) as exc:
        await outage_as_unavailable(
            _raise(AudibleAPIException("Audible search failed", upstream_status=429)),
            "No books found",
        )
    assert exc.value.message == "No books found"
    assert exc.value.upstream_status == 429


@pytest.mark.asyncio
async def test_outage_as_unavailable_chains_from_the_original_exception():
    original = AudibleAPIException("Audible search failed")
    with pytest.raises(AudibleAPIException) as exc:
        await outage_as_unavailable(_raise(original), "No books found")
    assert exc.value.__cause__ is original


@pytest.mark.asyncio
async def test_outage_as_unavailable_lets_a_genuine_not_found_propagate_unchanged():
    """A real NotFoundException (Audible answered and said no) is not this
    helper's concern at all -- it propagates as itself, not converted or
    reworded."""
    original = NotFoundException("Book not found: B000000001")
    with pytest.raises(NotFoundException) as exc:
        await outage_as_unavailable(_raise(original), "No books found")
    assert exc.value is original


def test_libex_core_audible_exception_keeps_its_502():
    """The 503 is the app's mapping; the package's own exception keeps 502."""
    assert AudibleAPIException("down").status_code == 502


# ============================================================
# THE HANDLER -- 503 for every AudibleAPIException, and only for it
# ============================================================

@pytest.mark.asyncio
async def test_handler_answers_an_audible_api_exception_with_the_full_503(async_client):
    with patch("app.api.routes.db.router.get_book_from_db", new_callable=AsyncMock) as mock:
        mock.side_effect = AudibleAPIException("Audible unavailable", upstream_status=502)
        response = await async_client.get(f"/db/book/{ASIN}")

    assert_outage_503(response, "Audible unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_status", [None, 429, 500, 502, 504])
async def test_handler_503_does_not_depend_on_the_upstream_status(async_client, upstream_status):
    with patch("app.api.routes.db.router.get_book_from_db", new_callable=AsyncMock) as mock:
        mock.side_effect = AudibleAPIException("down", upstream_status=upstream_status)
        response = await async_client.get(f"/db/book/{ASIN}")

    assert_outage_503(response, "down")


@pytest.mark.asyncio
async def test_handler_retry_after_header_and_body_agree(async_client):
    with patch("app.api.routes.db.router.get_book_from_db", new_callable=AsyncMock) as mock:
        mock.side_effect = AudibleAPIException("down")
        response = await async_client.get(f"/db/book/{ASIN}")

    assert int(response.headers["Retry-After"]) == response.json()["retryAfter"] == RETRY_AFTER_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize("path,status,code", [
    (f"/db/book/{ASIN}", 404, "not_in_libex"),
    ("/book/not-an-asin", 404, "invalid_request"),
    ("/db/book", 404, "invalid_request"),
])
async def test_404_body_has_no_retry_after_and_no_outage_headers(async_client, path, status, code):
    with patch("app.api.routes.db.router.get_book_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = None
        response = await async_client.get(path)

    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error", "status_code", "code"}
    assert body["code"] == code
    assert "retry-after" not in response.headers
    assert response.headers.get("cache-control") != "no-store"


@pytest.mark.asyncio
async def test_400_body_has_no_retry_after_and_no_outage_headers(async_client):
    response = await async_client.get(f"/book/{ASIN}?region=zz")

    assert response.status_code == 400
    assert response.json() == {
        "error": "Invalid region: zz",
        "status_code": 400,
        "code": "invalid_request",
    }
    assert "retry-after" not in response.headers
    assert response.headers.get("cache-control") != "no-store"


@pytest.mark.asyncio
async def test_cache_exception_stays_500_without_outage_shape(async_client):
    with patch("app.api.routes.db.router.get_book_from_db", new_callable=AsyncMock) as mock:
        mock.side_effect = CacheException("cache down")
        response = await async_client.get(f"/db/book/{ASIN}")

    assert response.status_code == 500
    assert response.json() == {
        "error": "cache down",
        "status_code": 500,
        "code": "upstream_unavailable",
    }
    assert "retry-after" not in response.headers
    assert response.headers.get("cache-control") != "no-store"
