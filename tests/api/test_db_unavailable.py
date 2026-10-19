"""
A failed database read on a route that answers from the stored catalog alone
is a 503 with a retry hint. A record that is genuinely not stored is still a
404 not_in_libex.

The session is replaced with one whose every call fails, so the real readers
run and the failure travels the whole route.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from httpx import ASGITransport, AsyncClient

# Local
from app.db.session import get_session
from app.main import app

ASIN = "B08G9PRS1K"

# (url, name the route module binds its reader to)
DB_ONLY_ROUTES = [
    ("/db/book?title=x", "app.api.routes.db.router.search_books_from_db"),
    ("/db/plans", "app.api.routes.db.router.get_distinct_plans_from_db"),
    ("/db/genres", "app.api.routes.db.router.get_distinct_genres_from_db"),
    ("/db/plans/Minerva", "app.api.routes.db.router.get_books_by_plan_from_db"),
    ("/db/vvab", "app.api.routes.db.router.get_vvab_books_from_db"),
    ("/db/new-releases", "app.api.routes.db.router.get_new_releases_from_db"),
    ("/db/coming-soon", "app.api.routes.db.router.get_coming_soon_from_db"),
    ("/db/book/sku/BK_1", "app.api.routes.db.router.get_books_by_sku_from_db"),
    (f"/db/book/{ASIN}/chapters", "app.api.routes.db.router.get_track_from_db"),
    (f"/db/book/{ASIN}", "app.api.routes.db.router.get_book_from_db"),
    (f"/db/author/{ASIN}/books", "app.api.routes.db.router.get_author_books_from_db"),
    (f"/db/author/{ASIN}", "app.api.routes.db.router.get_author_from_db"),
    ("/db/narrator/books?name=x", "app.api.routes.db.router.get_narrator_books_from_db"),
    ("/db/narrator?name=x", "app.api.routes.db.router.search_narrators_from_db"),
    (f"/db/series/{ASIN}/books", "app.api.routes.db.router.get_series_books_from_db"),
    (f"/db/series/{ASIN}", "app.api.routes.db.router.get_series_from_db"),
    ("/book/sku/BK_1", "app.api.routes.books.router.get_books_by_sku_from_db"),
]


class BrokenSession:
    """Every database call the readers make fails."""

    def __getattr__(self, name):
        async def fail(*args, **kwargs):
            raise ConnectionError("database down")

        return fail


@pytest.fixture
async def client():
    async def broken():
        yield BrokenSession()

    app.dependency_overrides[get_session] = broken
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.mark.parametrize("url,_target", DB_ONLY_ROUTES)
async def test_failed_read_is_503_with_retry_headers(client, url, _target):
    response = await client.get(url)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["status_code"] == 503
    assert body["code"] == "upstream_unavailable"
    assert body["retryAfter"] == 30
    assert "database down" not in response.text


@pytest.mark.parametrize("url,target", DB_ONLY_ROUTES)
async def test_missing_record_is_still_404_not_in_libex(client, url, target):
    empty = None if any(k in target for k in ("get_book_from_db", "get_track", "get_author_from_db", "get_series_from_db")) else []
    with patch(target, new_callable=AsyncMock, return_value=empty):
        response = await client.get(url)

    assert response.status_code == 404
    assert response.json()["code"] == "not_in_libex"
