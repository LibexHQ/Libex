"""
Each legacy path is registered on the same handler as its primary, so the same
request must produce the same status, headers and body on both. A divergence
here means one of the two has grown its own copy of the handler again.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
from app.main import app
from app.services.audible.authors import AuthorBooksResult
from libex_core.exceptions import AudibleAPIException
from tests.api.test_authors import MOCK_BOOK
from tests.api.test_db import MOCK_CHAPTERS, MOCK_SERIES

# Headers that legitimately differ between any two requests.
_PER_REQUEST_HEADERS = {"date", "x-request-id"}

ASIN = "B000APF21M"
BOOK_ASIN = "B08G9PRS1K"

# (primary path, legacy path, patch target, patch kwargs)
_CASES = {
    "author_books_complete": (
        f"/author/books/{ASIN}",
        f"/author/{ASIN}/books",
        {
            "app.api.routes.authors.router.get_author_books": {"return_value": AuthorBooksResult([BOOK_ASIN], True)},
            "app.api.routes.authors.router.get_books_by_asins": {"return_value": [MOCK_BOOK]},
        },
    ),
    "author_books_truncated": (
        f"/author/books/{ASIN}",
        f"/author/{ASIN}/books",
        {
            "app.api.routes.authors.router.get_author_books": {"return_value": AuthorBooksResult([BOOK_ASIN], False)},
            "app.api.routes.authors.router.get_books_by_asins": {"return_value": [MOCK_BOOK]},
        },
    ),
    "author_books_filtered_sorted": (
        f"/author/books/{ASIN}?cache=false&sort=title&order=desc",
        f"/author/{ASIN}/books?cache=false&sort=title&order=desc",
        {
            "app.api.routes.authors.router.get_author_books": {"return_value": AuthorBooksResult([BOOK_ASIN], True)},
            "app.api.routes.authors.router.get_books_by_asins": {"return_value": [MOCK_BOOK]},
        },
    ),
    "author_books_none": (
        f"/author/books/{ASIN}",
        f"/author/{ASIN}/books",
        {"app.api.routes.authors.router.get_author_books": {"return_value": AuthorBooksResult([], True)}},
    ),
    "author_books_outage": (
        f"/author/books/{ASIN}",
        f"/author/{ASIN}/books",
        {"app.api.routes.authors.router.get_author_books": {"side_effect": AudibleAPIException("down")}},
    ),
    "series_books": (
        "/series/books/B00SERIES1",
        "/series/B00SERIES1/books",
        {
            "app.api.routes.series.router.get_series_books": {"return_value": [BOOK_ASIN]},
            "app.api.routes.series.router.get_books_by_asins": {"return_value": [MOCK_BOOK]},
        },
    ),
    "series_books_none": (
        "/series/books/B00SERIES1",
        "/series/B00SERIES1/books",
        {"app.api.routes.series.router.get_series_books": {"return_value": []}},
    ),
    "series_books_outage": (
        "/series/books/B00SERIES1",
        "/series/B00SERIES1/books",
        {"app.api.routes.series.router.get_series_books": {"side_effect": AudibleAPIException("down")}},
    ),
    "series_search": (
        "/series/search?name=dune",
        "/series?name=dune",
        {"app.api.routes.series.router.search_series": {"return_value": [MOCK_SERIES]}},
    ),
    "series_search_none": (
        "/series/search?name=dune",
        "/series?name=dune",
        {"app.api.routes.series.router.search_series": {"return_value": []}},
    ),
    "series_search_missing_name": ("/series/search", "/series", {}),
    "chapters": (
        f"/book/{BOOK_ASIN}/chapters",
        f"/book/chapters/{BOOK_ASIN}",
        {"app.api.routes.books.router.get_chapters": {"return_value": MOCK_CHAPTERS}},
    ),
    "chapters_outage": (
        f"/book/{BOOK_ASIN}/chapters",
        f"/book/chapters/{BOOK_ASIN}",
        {"app.api.routes.books.router.get_chapters": {"side_effect": AudibleAPIException("down")}},
    ),
    "chapters_bad_region": (
        f"/book/{BOOK_ASIN}/chapters?region=xx",
        f"/book/chapters/{BOOK_ASIN}?region=xx",
        {},
    ),
}


async def _get(async_client, path, patches):
    from contextlib import ExitStack

    with ExitStack() as stack:
        for target, kwargs in patches.items():
            stack.enter_context(patch(target, new_callable=AsyncMock, **kwargs))
        return await async_client.get(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_CASES))
async def test_legacy_path_answers_exactly_like_its_primary(async_client, case):
    primary, legacy, patches = _CASES[case]

    first = await _get(async_client, primary, patches)
    second = await _get(async_client, legacy, patches)

    assert first.status_code == second.status_code
    assert first.content == second.content

    def headers(r):
        return {k: v for k, v in r.headers.items() if k not in _PER_REQUEST_HEADERS}

    assert headers(first) == headers(second)


def test_the_legacy_paths_stay_out_of_the_schema():
    paths = app.openapi()["paths"]
    for legacy in (
        "/author/{asin}/books",
        "/series/{asin}/books",
        "/series",
        "/book/chapters/{asin}",
    ):
        assert legacy not in paths
    for primary in (
        "/author/books/{asin}",
        "/series/books/{asin}",
        "/series/search",
        "/book/{asin}/chapters",
    ):
        assert primary in paths
