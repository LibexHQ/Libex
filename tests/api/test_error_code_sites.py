"""
Per-raise-site pins for the `code` on every NotFoundException a route raises.

Each case drives one raise site under app/api/routes through the real handler
and asserts the code on the wire, so a site whose code= is dropped or changed
fails by name. The tables are exhaustive for app/api/routes/**: 17 sites in
db/router.py, 4 in books/router.py, 7 in search/router.py, 4 in series, 4 in
authors, 1 in narrators and 3 in releases. Sites that rely on the class
default (not_on_audible) are pinned too, since the default is what they mean.

The outage rows at the end pin the other half: every live route's
AudibleAPIException answers 503 with `upstream_unavailable`, Retry-After and
no-store, carrying the route's own literal where the route has one.
"""

# Standard library
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
from libex_core.exceptions import AudibleAPIException
from tests.fixtures.outage import assert_outage_503

ASIN = "B08G9PRS1K"
DB = "app.api.routes.db.router."


# ============================================================
# db/router.py -- all 17 sites
# ============================================================

# (id, path, service patched at the router's import, value the service returns,
#  expected code, expected message)
_DB_SITES = [
    ("db_no_search_params", "/db/book", None, None, "invalid_request", "No search parameters provided"),
    ("db_search_empty", "/db/book?title=zzz", "search_books_from_db", [], "not_in_libex", "No books found matching the given parameters"),
    ("db_plans_empty", "/db/plans", "get_distinct_plans_from_db", [], "not_in_libex", "No plans found in local database"),
    ("db_genres_empty", "/db/genres", "get_distinct_genres_from_db", [], "not_in_libex", "No genres found in local database"),
    ("db_plan_books_empty", "/db/plans/Nope", "get_books_by_plan_from_db", [], "not_in_libex", "No books found for plan: Nope"),
    ("db_vvab_empty", "/db/vvab", "get_vvab_books_from_db", [], "not_in_libex", "No virtual voice audiobooks found in local database"),
    ("db_new_releases_empty", "/db/new-releases", "get_new_releases_from_db", [], "not_in_libex", "No new releases found in local database"),
    ("db_coming_soon_empty", "/db/coming-soon", "get_coming_soon_from_db", [], "not_in_libex", "No upcoming releases found in local database"),
    ("db_sku_empty", "/db/book/sku/BK_X", "get_books_by_sku_from_db", [], "not_in_libex", "No books found for SKU"),
    ("db_chapters_missing", f"/db/book/{ASIN}/chapters", "get_track_from_db", None, "not_in_libex", "No chapter data found for this book"),
    ("db_book_missing", f"/db/book/{ASIN}", "get_book_from_db", None, "not_in_libex", "Book not found in local database"),
    ("db_author_books_empty", f"/db/author/{ASIN}/books", "get_author_books_from_db", [], "not_in_libex", "No books found for author"),
    ("db_author_missing", f"/db/author/{ASIN}", "get_author_from_db", None, "not_in_libex", "Author not found in local database"),
    ("db_narrator_books_empty", "/db/narrator/books?name=Nobody", "get_narrator_books_from_db", [], "not_in_libex", "No books found for narrator: Nobody"),
    ("db_narrator_search_empty", "/db/narrator?name=Nobody", "search_narrators_from_db", [], "not_in_libex", "No narrators found matching: Nobody"),
    ("db_series_books_empty", f"/db/series/{ASIN}/books", "get_series_books_from_db", [], "not_in_libex", "No books found for series"),
    ("db_series_missing", f"/db/series/{ASIN}", "get_series_from_db", None, "not_in_libex", "Series not found in local database"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,service,value,code,message",
    [c[1:] for c in _DB_SITES],
    ids=[c[0] for c in _DB_SITES],
)
async def test_db_raise_site_code(async_client, path, service, value, code, message):
    if service is None:
        response = await async_client.get(path)
    else:
        with patch(DB + service, new_callable=AsyncMock) as mock:
            mock.return_value = value
            response = await async_client.get(path)

    assert response.status_code == 404
    assert response.json() == {"error": message, "status_code": 404, "code": code}


def test_db_table_covers_all_seventeen_sites():
    assert len(_DB_SITES) == 17


# ============================================================
# books/router.py
# ============================================================

@pytest.mark.asyncio
async def test_books_sku_miss_is_not_in_libex(async_client):
    with patch("app.api.routes.books.router.get_books_by_sku_from_db", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/book/sku/BK_X")

    assert response.status_code == 404
    assert response.json() == {
        "error": "No books found for SKU: BK_X",
        "status_code": 404,
        "code": "not_in_libex",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("path,message", [
    pytest.param(f"/book?asins={ASIN},bad", "Invalid ASIN format: bad", id="invalid_asin"),
    pytest.param("/book?asins=,", "No valid ASINs provided", id="no_valid_asins"),
    pytest.param(
        "/book?asins=" + ",".join(f"B{i:09d}" for i in range(1001)),
        "Maximum 1000 ASINs per request",
        id="over_1000_cap",
    ),
])
async def test_books_bulk_input_errors_are_invalid_request(async_client, path, message):
    with patch("app.api.routes.books.router.get_books_by_asins", new_callable=AsyncMock) as mock:
        response = await async_client.get(path)
        mock.assert_not_awaited()

    assert response.status_code == 404
    assert response.json() == {"error": message, "status_code": 404, "code": "invalid_request"}


@pytest.mark.asyncio
async def test_books_cap_boundary_1000_is_not_rejected(async_client):
    path = "/book?asins=" + ",".join(f"B{i:09d}" for i in range(1000))
    with patch("app.api.routes.books.router.get_books_by_asins", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get(path)
        mock.assert_awaited_once()

    assert response.json().get("code") != "invalid_request"


# ============================================================
# search/router.py, narrators/router.py
# ============================================================

SEARCH = "app.api.routes.search.router."


@pytest.mark.asyncio
@pytest.mark.parametrize("path,service,code,message", [
    pytest.param("/search?title=x", "search", "not_on_audible", "No books found", id="search_empty"),
    pytest.param("/quick-search?keywords=x", "quick_search", "not_on_audible", "No books found", id="quick_search_empty"),
    pytest.param("/us/search?title=x", "search", "not_on_audible", "No books found", id="abs_search_empty"),
    pytest.param("/us/quick-search/search?keywords=x", "quick_search", "not_on_audible", "No books found", id="abs_quick_search_empty"),
    pytest.param("/zz/search?title=x", None, "invalid_request", "Invalid region: zz", id="abs_search_bad_region"),
    pytest.param("/zz/quick-search/search?keywords=x", None, "invalid_request", "Invalid region: zz", id="abs_quick_bad_region"),
    pytest.param("/us/quick-search/search", None, "invalid_request", "No search terms provided", id="abs_quick_no_terms"),
])
async def test_search_raise_site_code(async_client, path, service, code, message):
    if service is None:
        response = await async_client.get(path)
    else:
        with patch(SEARCH + service, new_callable=AsyncMock) as mock:
            mock.return_value = []
            response = await async_client.get(path)

    assert response.status_code == 404
    assert response.json() == {"error": message, "status_code": 404, "code": code}


@pytest.mark.asyncio
async def test_narrator_books_empty_is_not_on_audible(async_client):
    with patch("app.api.routes.narrators.router.search", new_callable=AsyncMock) as mock:
        mock.return_value = []
        response = await async_client.get("/narrator/books?name=Nobody")

    assert response.status_code == 404
    assert response.json() == {
        "error": "No books found for narrator: Nobody",
        "status_code": 404,
        "code": "not_on_audible",
    }


# ============================================================
# series/router.py, authors/router.py, releases/router.py
# ============================================================

_EMPTY_WALK = SimpleNamespace(asins=[], is_complete=True, cache_expires_at=None)

# (id, path, router module, service name, value returned, message)
_LIVE_EMPTY_SITES = [
    ("series_search", "/series/search?name=x", "series", "search_series", [], "No series found"),
    ("series_search_legacy", "/series?name=x", "series", "search_series", [], "No series found"),
    ("series_books", f"/series/books/{ASIN}", "series", "get_series_books", [], "No books found for series"),
    ("series_books_legacy", f"/series/{ASIN}/books", "series", "get_series_books", [], "No books found for series"),
    ("authors_search", "/author?name=x", "authors", "search_authors", [], "No authors found"),
    ("authors_books_by_name", "/author/books?name=x", "authors", "get_author_books_by_name", [], "No books found for author"),
    ("authors_books", f"/author/books/{ASIN}", "authors", "get_author_books", _EMPTY_WALK, "No books found for author"),
    ("authors_books_legacy", f"/author/{ASIN}/books", "authors", "get_author_books", _EMPTY_WALK, "No books found for author"),
    ("releases_new", "/new-releases", "releases", "get_new_releases", [], "No new releases found"),
    ("releases_coming_soon", "/coming-soon", "releases", "get_coming_soon", [], "No upcoming releases found"),
    ("releases_categories", "/categories", "releases", "ensure_genres", [], "No categories available"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,module,service,value,message",
    [c[1:] for c in _LIVE_EMPTY_SITES],
    ids=[c[0] for c in _LIVE_EMPTY_SITES],
)
async def test_live_route_genuine_empty_is_not_on_audible(async_client, path, module, service, value, message):
    with patch(f"app.api.routes.{module}.router.{service}", new_callable=AsyncMock) as mock:
        mock.return_value = value
        response = await async_client.get(path)

    assert response.status_code == 404
    assert response.json() == {"error": message, "status_code": 404, "code": "not_on_audible"}


# ============================================================
# Audible outage -- every live route answers 503 upstream_unavailable
# ============================================================

# (id, path, router module, service name, message the 503 carries). A route
# with its own literal for the failed lookup shows that literal; the rest show
# the service's message.
_SERVICE_MESSAGE = "Audible service failure"
_OUTAGE_SITES = [
    ("book", f"/book/{ASIN}", "books", "get_book_by_asin", _SERVICE_MESSAGE),
    ("book_chapters", f"/book/{ASIN}/chapters", "books", "get_chapters", _SERVICE_MESSAGE),
    ("book_chapters_legacy", f"/book/chapters/{ASIN}", "books", "get_chapters", _SERVICE_MESSAGE),
    ("book_bulk", f"/book?asins={ASIN}", "books", "get_books_by_asins", _SERVICE_MESSAGE),
    ("search", "/search?title=x", "search", "search", "No books found"),
    ("quick_search", "/quick-search?keywords=x", "search", "quick_search", "No books found"),
    ("abs_search", "/us/search?title=x", "search", "search", "No books found"),
    ("abs_quick_search", "/us/quick-search/search?keywords=x", "search", "quick_search", "No books found"),
    ("narrator_books", "/narrator/books?name=Nobody", "narrators", "search", "No books found for narrator: Nobody"),
    ("series_search", "/series/search?name=x", "series", "search_series", _SERVICE_MESSAGE),
    ("series_search_legacy", "/series?name=x", "series", "search_series", _SERVICE_MESSAGE),
    ("series_books", f"/series/books/{ASIN}", "series", "get_series_books", _SERVICE_MESSAGE),
    ("series_books_legacy", f"/series/{ASIN}/books", "series", "get_series_books", _SERVICE_MESSAGE),
    ("series", f"/series/{ASIN}", "series", "get_series", _SERVICE_MESSAGE),
    ("authors_search", "/author?name=x", "authors", "search_authors", _SERVICE_MESSAGE),
    ("authors_books_by_name", "/author/books?name=x", "authors", "get_author_books_by_name", _SERVICE_MESSAGE),
    ("authors_books", f"/author/books/{ASIN}", "authors", "get_author_books", _SERVICE_MESSAGE),
    ("authors_books_legacy", f"/author/{ASIN}/books", "authors", "get_author_books", _SERVICE_MESSAGE),
    ("author", f"/author/{ASIN}", "authors", "get_author", _SERVICE_MESSAGE),
    ("releases_new", "/new-releases", "releases", "get_new_releases", "No new releases found"),
    ("releases_coming_soon", "/coming-soon", "releases", "get_coming_soon", "No upcoming releases found"),
    ("releases_categories", "/categories", "releases", "ensure_genres", "No categories available"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,module,service,message",
    [c[1:] for c in _OUTAGE_SITES],
    ids=[c[0] for c in _OUTAGE_SITES],
)
async def test_live_route_outage_is_503_upstream_unavailable(async_client, path, module, service, message):
    with patch(f"app.api.routes.{module}.router.{service}", new_callable=AsyncMock) as mock:
        mock.side_effect = AudibleAPIException(_SERVICE_MESSAGE)
        response = await async_client.get(path)

    assert_outage_503(response, message)
