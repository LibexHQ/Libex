"""
Tests for the machine-readable `code` carried in the error envelope.

The status code says how a request failed; `code` says whose gap it is. These
pin the public vocabulary, that every code is emitted by a real route path (not
a hand-built exception), that the same-status reasons are told apart, that an
unhandled 500 deliberately carries none, and that OpenAPI documents the
envelope.
"""

# Standard library
import importlib
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from fastapi.testclient import TestClient

# Local
import app.main as main_module
from app.core.config import get_settings
from app.main import app
from libex_core.exceptions import (
    AudibleAPIException,
    CacheException,
    ErrorCode,
    NotFoundException,
)
from tests.api.test_books import _cache_route_product, _placeholder_audible_product

ASIN = "B08G9PRS1K"


# ============================================================
# VOCABULARY
# ============================================================

def test_error_code_values_are_exactly_the_five_public_strings():
    assert {c.value for c in ErrorCode} == {
        "not_in_libex",
        "not_on_audible",
        "withheld",
        "upstream_unavailable",
        "invalid_request",
    }
    assert len(ErrorCode) == 5


# ============================================================
# EACH CODE FROM A REAL ROUTE PATH
# ============================================================

@pytest.mark.asyncio
async def test_not_in_libex_from_db_sku_miss(async_client):
    with patch("app.api.routes.db.router.get_books_by_sku_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/db/book/sku/BK_FAKE_000000")

    assert response.status_code == 404
    assert response.json()["code"] == "not_in_libex"


@pytest.mark.asyncio
async def test_not_in_libex_from_db_book_miss(async_client):
    with patch("app.api.routes.db.router.get_book_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = None
        response = await async_client.get(f"/db/book/{ASIN}")

    assert response.status_code == 404
    assert response.json() == {
        "error": "Book not found in local database",
        "status_code": 404,
        "code": "not_in_libex",
    }


@pytest.mark.asyncio
async def test_not_in_libex_from_book_sku_miss(async_client):
    with patch("app.api.routes.books.router.get_books_by_sku", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/book/sku/BK_FAKE_000000")

    assert response.status_code == 404
    assert response.json()["code"] == "not_in_libex"


@pytest.mark.asyncio
async def test_not_on_audible_from_genuine_single_book_miss(async_client):
    """Audible answered and has no such book: end to end through the real
    service, not a raised NotFoundException."""
    with patch("app.services.audible.books.audible_get",
               new=AsyncMock(return_value={"products": []})), \
         patch("app.services.audible.books.persist_books_background"):
        response = await async_client.get(f"/book/{ASIN}?cache=false")

    assert response.status_code == 404
    assert response.json() == {
        "error": f"Book not found: {ASIN}",
        "status_code": 404,
        "code": "not_on_audible",
    }


@pytest.mark.asyncio
async def test_withheld_from_single_book_placeholder(async_client):
    placeholder = "B0PLACE001"
    with patch("app.services.audible.books.audible_get",
               new=AsyncMock(return_value={"product": _placeholder_audible_product(placeholder)})), \
         patch("app.services.audible.books.persist_books_background"):
        response = await async_client.get(f"/book/{placeholder}?cache=false")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "withheld"
    assert body["status_code"] == 404
    assert placeholder in body["error"]


@pytest.mark.asyncio
async def test_placeholder_and_genuine_miss_share_status_but_not_code(async_client):
    placeholder = "B0PLACE001"
    with patch("app.services.audible.books.audible_get",
               new=AsyncMock(return_value={"product": _placeholder_audible_product(placeholder)})), \
         patch("app.services.audible.books.persist_books_background"):
        withheld = await async_client.get(f"/book/{placeholder}?cache=false")
    with patch("app.services.audible.books.audible_get",
               new=AsyncMock(return_value={"products": []})), \
         patch("app.services.audible.books.persist_books_background"):
        missing = await async_client.get(f"/book/{ASIN}?cache=false")

    assert withheld.status_code == missing.status_code == 404
    assert withheld.json()["code"] == "withheld"
    assert missing.json()["code"] == "not_on_audible"


@pytest.mark.asyncio
async def test_real_product_is_not_withheld(async_client):
    with patch("app.services.audible.books.audible_get",
               new=AsyncMock(return_value={"product": _cache_route_product(ASIN)})), \
         patch("app.services.audible.books.persist_books_background"):
        response = await async_client.get(f"/book/{ASIN}?cache=false")

    assert response.status_code == 200
    assert "code" not in response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("path,target,kwargs", [
    (f"/book/{ASIN}", "app.api.routes.books.router.get_book_by_asin", {}),
    (f"/book/{ASIN}/chapters", "app.api.routes.books.router.get_chapters", {}),
    ("/book?asins=" + ASIN, "app.api.routes.books.router.get_books_by_asins", {}),
    ("/search?title=Dune", "app.api.routes.search.router.search", {}),
])
async def test_upstream_unavailable_from_outage_keeps_404(async_client, path, target, kwargs):
    with patch(target, new_callable=AsyncMock) as mock:
        mock.side_effect = AudibleAPIException("Audible unavailable")
        response = await async_client.get(path)

    assert response.status_code == 404
    assert response.json()["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_outage_and_genuine_absence_share_status_but_not_code(async_client):
    path = f"/book/{ASIN}"
    with patch("app.api.routes.books.router.get_book_by_asin", new_callable=AsyncMock) as mock:
        mock.side_effect = AudibleAPIException("down")
        outage = await async_client.get(path)
    with patch("app.api.routes.books.router.get_book_by_asin", new_callable=AsyncMock) as mock:
        mock.side_effect = NotFoundException(f"Book not found: {ASIN}")
        absent = await async_client.get(path)

    assert outage.status_code == absent.status_code == 404
    assert outage.json()["code"] == "upstream_unavailable"
    assert absent.json()["code"] == "not_on_audible"


@pytest.mark.asyncio
async def test_raw_audible_api_exception_reaches_handler_as_upstream_unavailable(async_client):
    """No outage_as_not_found wrap on /db routes: the exception reaches the
    handler itself, which keeps its 502 and carries the class default code."""
    with patch("app.api.routes.db.router.get_book_from_db", new_callable=AsyncMock) as mock:
        mock.side_effect = AudibleAPIException("Audible unavailable")
        response = await async_client.get(f"/db/book/{ASIN}")

    assert response.status_code == 502
    assert response.json() == {
        "error": "Audible unavailable",
        "status_code": 502,
        "code": "upstream_unavailable",
    }


@pytest.mark.asyncio
async def test_raw_cache_exception_reaches_handler_as_upstream_unavailable(async_client):
    with patch("app.api.routes.db.router.get_book_from_db", new_callable=AsyncMock) as mock:
        mock.side_effect = CacheException("cache down")
        response = await async_client.get(f"/db/book/{ASIN}")

    assert response.status_code == 500
    assert response.json()["code"] == "upstream_unavailable"


# ============================================================
# invalid_request -- STATUS UNCHANGED
# ============================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/book/not-an-asin",
    "/author/not-an-asin",
    "/series/not-an-asin",
    "/db/book/not-an-asin",
])
async def test_invalid_asin_is_invalid_request_and_still_404(async_client, path):
    response = await async_client.get(path)

    assert response.status_code == 404
    assert response.json() == {
        "error": "Invalid ASIN format: not-an-asin",
        "status_code": 404,
        "code": "invalid_request",
    }


@pytest.mark.asyncio
async def test_invalid_region_is_invalid_request_and_still_400(async_client):
    response = await async_client.get(f"/book/{ASIN}?region=zz")

    assert response.status_code == 400
    assert response.json() == {
        "error": "Invalid region: zz",
        "status_code": 400,
        "code": "invalid_request",
    }


@pytest.mark.asyncio
async def test_bulk_invalid_asin_is_invalid_request(async_client):
    response = await async_client.get(f"/book?asins={ASIN},not-an-asin")

    assert response.status_code == 404
    assert response.json()["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_db_search_without_parameters_is_invalid_request(async_client):
    response = await async_client.get("/db/book")

    assert response.status_code == 404
    assert response.json()["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_search_without_terms_is_invalid_request(async_client):
    response = await async_client.get("/us/quick-search/search")

    assert response.status_code == 404
    assert response.json() == {
        "error": "No search terms provided",
        "status_code": 404,
        "code": "invalid_request",
    }


@pytest.mark.asyncio
async def test_search_invalid_region_is_invalid_request(async_client):
    response = await async_client.get("/zz/quick-search/search?keywords=dune")

    assert response.status_code == 404
    assert response.json()["code"] == "invalid_request"


# ============================================================
# UNHANDLED 500 -- NO CODE
# ============================================================

def test_unhandled_500_carries_no_code():
    get_settings.cache_clear()
    importlib.reload(main_module)
    try:
        fresh = main_module.app

        @fresh.get("/test-boom-code")
        def boom():
            raise RuntimeError("boom")

        response = TestClient(fresh, raise_server_exceptions=False).get("/test-boom-code")
    finally:
        get_settings.cache_clear()
        importlib.reload(main_module)
        get_settings.cache_clear()

    assert response.status_code == 500
    assert "code" not in response.json()


# ============================================================
# OPENAPI
# ============================================================

def test_openapi_error_response_schema_uses_the_error_code_enum():
    schemas = app.openapi()["components"]["schemas"]

    assert schemas["ErrorCode"]["enum"] == [
        "not_in_libex",
        "not_on_audible",
        "withheld",
        "upstream_unavailable",
        "invalid_request",
    ]
    error = schemas["ErrorResponse"]
    assert error["properties"]["code"]["$ref"] == "#/components/schemas/ErrorCode"
    assert set(error["required"]) == {"error", "status_code", "code"}


@pytest.mark.parametrize("path", [
    "/book/{asin}",
    "/book/{asin}/chapters",
    "/book/sku/{sku}",
    "/book",
    "/search",
    "/author/{asin}",
    "/series/{asin}",
    "/narrator/books",
    "/db/book/{asin}",
    "/db/book/sku/{sku}",
])
def test_openapi_documents_error_response_on_404(path):
    operation = app.openapi()["paths"][path]["get"]
    schema = operation["responses"]["404"]["content"]["application/json"]["schema"]

    assert schema == {"$ref": "#/components/schemas/ErrorResponse"}
