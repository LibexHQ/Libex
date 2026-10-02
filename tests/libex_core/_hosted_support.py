"""
A way to ask a hosted route with Audible answered by a stand-in `get`
and every other place the route could get an answer from (the cache, the
database, the background writers) replaced by one that says nothing, so the
body that comes back is only what `get` made of it. The `hosted` fixture in
conftest.py wraps it, for the tests that hold the library and the command line
to the routes' own answers.
"""

# Standard library
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, patch

# Local
from app.db.session import get_session
from app.main import app
from tests.libex_core._lookup_support import STORED_GENRES

HOSTED_MODULES = (
    "app.services.audible.books",
    "app.services.audible.search",
    "app.services.audible.series",
    "app.services.audible.releases",
    "app.services.audible.authors.screens",
    "app.services.audible.authors.catalog",
    "app.services.audible.authors.by_name",
    "app.services.audible.authors.profile",
)

PERSIST_HOOKS = (
    "app.services.audible.books.persist_books_background",
    "app.services.audible.books.persist_track_background",
    "app.services.audible.search.persist_books_background",
    "app.services.audible.releases.persist_books_background",
    "app.services.audible.series.persist_series_background",
    "app.services.audible.series.persist_cache_background",
    "app.services.audible.authors.profile.persist_author_background",
    "app.services.audible.authors.persist_author_background",
    "app.services.audible.authors.persist_author_books_cache_background",
    "app.services.audible.authors.request_author_books_completion",
)


def _nothing_stored():
    return [
        ("app.services.cache.manager.get", AsyncMock(return_value=None)),
        ("app.services.cache.manager.get_many", AsyncMock(return_value={})),
        ("app.services.cache.manager.get_entry", AsyncMock(return_value=None)),
        ("app.services.cache.manager.set", AsyncMock()),
        ("app.services.audible.books.get_books_from_db", AsyncMock(return_value=[])),
        ("app.services.audible.books.get_track_from_db", AsyncMock(return_value=None)),
        ("app.services.audible.search.search_books_from_db", AsyncMock(return_value=[])),
        ("app.services.audible.series.get_series_from_db", AsyncMock(return_value=None)),
        ("app.services.audible.series.search_series_from_db", AsyncMock(return_value=[])),
        ("app.services.audible.authors.profile.get_author_from_db", AsyncMock(return_value=None)),
        ("app.services.audible.authors.get_author_from_db", AsyncMock(return_value=None)),
        ("app.services.audible.authors.get_author_book_asins_from_db", AsyncMock(return_value=[])),
        # The taxonomy is read from the database first and refreshed from
        # Audible; the second read is the refreshed copy.
        ("app.services.audible.releases.get_stored_genres",
         AsyncMock(side_effect=[([], None), (STORED_GENRES, None)])),
        ("app.services.audible.releases.reconcile_genres", AsyncMock()),
        ("app.services.audible.releases.upsert_genres", AsyncMock()),
    ]


@contextmanager
def hosted_asker(client):
    """Yields ask(get, path, params), which calls a hosted route and returns
    the response. The cache=false the routes take keeps every read off the
    cache. After a call, ask.hooks maps each background writer to its mock, so
    a test can say what the route tried to store."""
    app.dependency_overrides[get_session] = lambda: AsyncMock()

    def ask(get, path, params):
        hooks = {}
        ask.hooks = hooks
        with ExitStack() as stack:
            for module in HOSTED_MODULES:
                stack.enter_context(patch(f"{module}.audible_get", new=get))
            for target, stand_in in _nothing_stored():
                stack.enter_context(patch(target, new=stand_in))
            for target in PERSIST_HOOKS:
                hooks[target] = stack.enter_context(patch(target))
            return client.get(path, params={**params, "cache": "false"})

    try:
        yield ask
    finally:
        app.dependency_overrides.pop(get_session, None)
