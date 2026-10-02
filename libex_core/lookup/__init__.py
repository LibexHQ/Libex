"""
The lookups behind Libex's endpoints, as functions of the published models.

One function per endpoint, each taking the callable that makes the request (an
AudibleGet) first and the region as a keyword, each returning the model the
hosted route returns for the same Audible answer. They are the hosted live
path -- fetch, normalize, settle, split what Audible answered from what it did
not -- without the cache. Called alone, they persist nothing and an outage is
reported as one. Every one also takes a keyword-only store, a
libex_core.storage.LocalStore: with it the lookup writes what Audible answered
through under the hosted merge rules, serves the row the store then holds, and
answers an outage from the stored copy where the hosted service does. Nothing
about the storage is imported unless a store is passed, so this package works
without the storage extra.
"""

# Local
from libex_core.lookup.author_books import (
    INCOMPLETE_REASONS,
    AuthorBooks,
    get_author_books,
    get_author_books_by_name,
)
from libex_core.lookup.authors import get_author, search_authors
from libex_core.lookup.books import get_book, get_books, get_chapters
from libex_core.lookup.releases import RELEASE_WINDOWS, categories, coming_soon, new_releases
from libex_core.lookup.search import (
    abs_quick_search,
    abs_search,
    narrator_books,
    quick_search,
    search,
)
from libex_core.lookup.series import get_series, get_series_books, search_series

__all__ = [
    "INCOMPLETE_REASONS",
    "RELEASE_WINDOWS",
    "AuthorBooks",
    "abs_quick_search",
    "abs_search",
    "categories",
    "coming_soon",
    "get_author",
    "get_author_books",
    "get_author_books_by_name",
    "get_book",
    "get_books",
    "get_chapters",
    "get_series",
    "get_series_books",
    "narrator_books",
    "new_releases",
    "quick_search",
    "search",
    "search_authors",
    "search_series",
]
