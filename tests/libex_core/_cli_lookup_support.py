"""
Shared pieces for the lookup command tests: the table of commands with the
library call each one stands for, the stand-in session the commands run
against, and a way to ask the hosted routes the same question.
"""

# Standard library
import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock

# Local
from libex_core.lookup import (
    abs_quick_search,
    abs_search,
    categories,
    coming_soon,
    get_author,
    get_author_books,
    get_author_books_by_name,
    get_book,
    get_books,
    get_chapters,
    get_series,
    get_series_books,
    narrator_books,
    new_releases,
    quick_search,
    search,
    search_authors,
    search_series,
)
from tests.libex_core._lookup_support import (
    AUTHOR,
    AUTHOR_NAME,
    BOOKS,
    SERIES,
    fake_get,
)
from tests.libex_core.test_lookup import (
    _batch_get,
    _product,
    _search_get,
    _series_get,
    _suggestion_get,
)

# Not an ASIN of anything, and unlikely to appear in any fixed message.
PLANTED = "Zq9-distinctive-typed-text"

EGRESS = {"LIBEX_CORE_ALLOW_DIRECT_EGRESS": "1"}

CHAPTERS = {"content_metadata": {"chapter_info": {
    "is_accurate": True,
    "runtime_length_ms": 9000,
    "chapters": [
        {"length_ms": 5000, "start_offset_ms": 0, "title": "One"},
        {"length_ms": 4000, "start_offset_ms": 5000, "title": "Two"},
    ],
}}}


def chapters_get():
    return AsyncMock(return_value=CHAPTERS)


@dataclass(frozen=True)
class Case:
    """One command: the words that run it, the stand-in Audible it is run
    against, the library call it must equal, and the hosted route that
    answers the same question."""

    name: str
    argv: tuple[str, ...]
    make_get: Callable[[], Any]
    lookup: Callable[[Any, str], Any]
    route: Callable[[str], tuple[str, dict[str, Any]]]


def _bulk_get():
    return _batch_get(
        known={"B0LOOK0001", "B0LOOK0002"}, placeholders={"B0LOOK0003"}
    )


CASES = (
    Case(
        "book get",
        ("book", "get", "B0LOOK0001"),
        lambda: _batch_get(known={"B0LOOK0001"}),
        lambda get, region: get_book(get, "B0LOOK0001", region=region),
        lambda region: ("/book/B0LOOK0001", {"region": region}),
    ),
    Case(
        "book bulk",
        ("book", "bulk", "B0LOOK0001,B0LOOK0002", "B0LOOK0003", "B0LOOK0004"),
        _bulk_get,
        lambda get, region: get_books(
            get,
            ["B0LOOK0001", "B0LOOK0002", "B0LOOK0003", "B0LOOK0004"],
            region=region,
        ),
        lambda region: (
            "/book",
            {
                "asins": "B0LOOK0001,B0LOOK0002,B0LOOK0003,B0LOOK0004",
                "region": region,
            },
        ),
    ),
    Case(
        "book chapters",
        ("book", "chapters", "B0LOOK0001"),
        chapters_get,
        lambda get, region: get_chapters(get, "B0LOOK0001", region=region),
        lambda region: ("/book/B0LOOK0001/chapters", {"region": region}),
    ),
    Case(
        "series get",
        ("series", "get", "B0SERIES01"),
        lambda: _series_get(),
        lambda get, region: get_series(get, "B0SERIES01", region=region),
        lambda region: ("/series/B0SERIES01", {"region": region}),
    ),
    Case(
        "series books",
        ("series", "books", "B0SERIES01"),
        lambda: _series_get(["B0LOOK0002", "B0LOOK0001"]),
        lambda get, region: get_series_books(get, "B0SERIES01", region=region),
        lambda region: ("/series/books/B0SERIES01", {"region": region}),
    ),
    Case(
        "search",
        (
            "search", "--title", "hobbit", "--author", "tolkien", "--narrator",
            "inglis", "--publisher", "pub", "--keywords", "kw", "--sort-by",
            "Relevance", "--limit", "5", "--page", "2",
        ),
        lambda: _search_get([_product("B0LOOK0001"), _product("B0LOOK0002")]),
        lambda get, region: search(
            get, title="hobbit", author="tolkien", narrator="inglis",
            publisher="pub", keywords="kw", products_sort_by="Relevance",
            limit=5, page=2, region=region,
        ),
        lambda region: (
            "/search",
            {
                "title": "hobbit", "author": "tolkien", "narrator": "inglis",
                "publisher": "pub", "keywords": "kw",
                "products_sort_by": "Relevance", "limit": 5, "page": 2,
                "region": region,
            },
        ),
    ),
    Case(
        "search query",
        ("search", "--query", "hobbit"),
        lambda: _search_get([_product("B0LOOK0001")]),
        lambda get, region: search(get, query="hobbit", region=region),
        lambda region: ("/search", {"query": "hobbit", "region": region}),
    ),
    Case(
        "quick-search",
        ("quick-search", "hobbit"),
        lambda: _suggestion_get(["B0LOOK0001", "B0LOOK0002"]),
        lambda get, region: quick_search(get, "hobbit", region=region),
        lambda region: ("/quick-search", {"keywords": "hobbit", "region": region}),
    ),
    Case(
        "abs search",
        ("abs", "search", "--query", "hobbit", "--author", "tolkien"),
        lambda: _search_get([_product("B0LOOK0001"), _product("B0LOOK0002")]),
        lambda get, region: abs_search(
            get, query="hobbit", author="tolkien", region=region
        ),
        lambda region: (
            f"/{region}/search", {"query": "hobbit", "author": "tolkien"}
        ),
    ),
    Case(
        "abs quick-search",
        ("abs", "quick-search", "--keywords", "hobbit"),
        lambda: _suggestion_get(["B0LOOK0001", "B0LOOK0002"]),
        lambda get, region: abs_quick_search(get, keywords="hobbit", region=region),
        lambda region: (f"/{region}/quick-search/search", {"keywords": "hobbit"}),
    ),
    Case(
        "abs search title and keywords",
        ("abs", "search", "--title", "hobbit", "--keywords", "kw"),
        lambda: _search_get([_product("B0LOOK0001")]),
        lambda get, region: abs_search(
            get, title="hobbit", keywords="kw", region=region
        ),
        lambda region: (f"/{region}/search", {"title": "hobbit", "keywords": "kw"}),
    ),
    Case(
        "abs quick-search query",
        ("abs", "quick-search", "--query", "hobbit"),
        lambda: _suggestion_get(["B0LOOK0001"]),
        lambda get, region: abs_quick_search(get, query="hobbit", region=region),
        lambda region: (f"/{region}/quick-search/search", {"query": "hobbit"}),
    ),
    Case(
        "abs quick-search title",
        ("abs", "quick-search", "--title", "hobbit"),
        lambda: _suggestion_get(["B0LOOK0001"]),
        lambda get, region: abs_quick_search(get, title="hobbit", region=region),
        lambda region: (f"/{region}/quick-search/search", {"title": "hobbit"}),
    ),
    Case(
        "narrator books",
        ("narrator", "books", "Rob Inglis", "--limit", "3", "--page", "1"),
        lambda: _search_get([_product("B0LOOK0001")]),
        lambda get, region: narrator_books(
            get, "Rob Inglis", limit=3, page=1, region=region
        ),
        lambda region: (
            "/narrator/books",
            {"name": "Rob Inglis", "limit": 3, "page": 1, "region": region},
        ),
    ),
)

def _catalogue_get():
    """The stand-in for the commands that read an author's catalogue, a
    series' members or a release window, which asks for each of them in its
    own way."""
    return AsyncMock(side_effect=fake_get)


# The same filters, sort and order as command line flags, as the keywords the
# library takes, and as the hosted routes' query parameters.
SHAPE_ARGV = (
    "--longer-than", "150", "--language", "english",
    "--sort", "lengthMinutes", "--order", "desc",
)
SHAPE_KWARGS = {
    "filters": {"longer_than": 150, "language": "english"},
    "sort": "lengthMinutes",
    "order": "desc",
}
SHAPE_QUERY = {
    "longer_than": 150, "language": "english", "sort": "lengthMinutes", "order": "desc",
}
BULK_ASINS = [*BOOKS, "B0MISSING1"]

NEW_CASES = (
    Case(
        "series search",
        ("series", "search", "q"),
        _catalogue_get,
        lambda get, region: search_series(get, "q", region=region),
        lambda region: ("/series/search", {"name": "q", "region": region}),
    ),
    Case(
        "series books shaped",
        ("series", "books", SERIES, *SHAPE_ARGV),
        _catalogue_get,
        lambda get, region: get_series_books(get, SERIES, region=region, **SHAPE_KWARGS),
        lambda region: (
            f"/series/books/{SERIES}", {**SHAPE_QUERY, "region": region}
        ),
    ),
    Case(
        "book bulk shaped",
        ("book", "bulk", ",".join(BULK_ASINS), *SHAPE_ARGV),
        _catalogue_get,
        lambda get, region: get_books(get, BULK_ASINS, region=region, **SHAPE_KWARGS),
        lambda region: (
            "/book", {"asins": ",".join(BULK_ASINS), **SHAPE_QUERY, "region": region}
        ),
    ),
    Case(
        "author get",
        ("author", "get", AUTHOR),
        _catalogue_get,
        lambda get, region: get_author(get, AUTHOR, region=region),
        lambda region: (f"/author/{AUTHOR}", {"region": region}),
    ),
    Case(
        "author search",
        ("author", "search", "jane"),
        _catalogue_get,
        lambda get, region: search_authors(get, "jane", region=region),
        lambda region: ("/author", {"name": "jane", "region": region}),
    ),
    Case(
        "author books",
        ("author", "books", AUTHOR),
        _catalogue_get,
        lambda get, region: get_author_books(get, AUTHOR, region=region),
        lambda region: (f"/author/books/{AUTHOR}", {"region": region}),
    ),
    Case(
        "author books shaped",
        ("author", "books", AUTHOR, *SHAPE_ARGV),
        _catalogue_get,
        lambda get, region: get_author_books(
            get, AUTHOR, region=region, **SHAPE_KWARGS
        ),
        lambda region: (
            f"/author/books/{AUTHOR}", {**SHAPE_QUERY, "region": region}
        ),
    ),
    Case(
        "author books-by-name",
        ("author", "books-by-name", AUTHOR_NAME),
        _catalogue_get,
        lambda get, region: get_author_books_by_name(get, AUTHOR_NAME, region=region),
        lambda region: ("/author/books", {"name": AUTHOR_NAME, "region": region}),
    ),
    Case(
        "author books-by-name shaped",
        ("author", "books-by-name", AUTHOR_NAME, *SHAPE_ARGV),
        _catalogue_get,
        lambda get, region: get_author_books_by_name(
            get, AUTHOR_NAME, region=region, **SHAPE_KWARGS
        ),
        lambda region: (
            "/author/books", {"name": AUTHOR_NAME, **SHAPE_QUERY, "region": region}
        ),
    ),
    Case(
        "releases new",
        ("releases", "new", "--days", "60"),
        _catalogue_get,
        lambda get, region: new_releases(get, 60, region=region),
        lambda region: ("/new-releases", {"days": 60, "region": region}),
    ),
    Case(
        "releases new shaped",
        ("releases", "new", "--days", "365", *SHAPE_ARGV),
        _catalogue_get,
        lambda get, region: new_releases(get, 365, region=region, **SHAPE_KWARGS),
        lambda region: (
            "/new-releases", {"days": 365, **SHAPE_QUERY, "region": region}
        ),
    ),
    Case(
        "releases new category",
        ("releases", "new", "--days", "365", "--category", "123"),
        _catalogue_get,
        lambda get, region: new_releases(get, 365, "123", region=region),
        lambda region: (
            "/new-releases", {"days": 365, "category": "123", "region": region}
        ),
    ),
    Case(
        "releases coming-soon",
        ("releases", "coming-soon", "--days", "30"),
        _catalogue_get,
        lambda get, region: coming_soon(get, 30, region=region),
        lambda region: ("/coming-soon", {"days": 30, "region": region}),
    ),
    Case(
        "releases coming-soon shaped",
        ("releases", "coming-soon", "--sort", "lengthMinutes", "--order", "desc"),
        _catalogue_get,
        lambda get, region: coming_soon(
            get, 30, region=region, sort="lengthMinutes", order="desc"
        ),
        lambda region: (
            "/coming-soon",
            {"days": 30, "sort": "lengthMinutes", "order": "desc", "region": region},
        ),
    ),
    Case(
        "releases categories",
        ("releases", "categories"),
        _catalogue_get,
        lambda get, region: categories(get, region=region),
        lambda region: ("/categories", {"region": region}),
    ),
    Case(
        "releases categories flat depth",
        ("releases", "categories", "--flat", "--depth", "2"),
        _catalogue_get,
        lambda get, region: categories(get, region=region, flat=True, depth=2),
        lambda region: (
            "/categories", {"flat": "true", "depth": 2, "region": region}
        ),
    ),
)

# Commands that report a miss in what they print rather than failing on it.
BULK_NAMES = frozenset({"book bulk", "book bulk shaped"})

# An author's books by name read a first page that answers 404 as a failure to
# find out, which is an outage on the route (503) and on the command (status
# 4), not as the author having no books.
BY_NAME_NAMES = frozenset({"author books-by-name", "author books-by-name shaped"})

# What the routes answer where the command does not: Audible's 404 on the
# release and category scans is a 503 on the route, and status 3 here.
ROUTE_503_ON_AUDIBLE_404 = frozenset({
    "releases new", "releases new shaped", "releases new category",
    "releases coming-soon", "releases coming-soon shaped",
    "releases categories", "releases categories flat depth",
})

CASES = (*CASES, *NEW_CASES)

# The commands where Audible answering 404 is a 404 on the route and status 3
# on the command, which is every command not named above.
PLAIN_NOT_FOUND_CASES = [
    c for c in CASES
    if c.name not in BULK_NAMES | BY_NAME_NAMES | ROUTE_503_ON_AUDIBLE_404
]


CASE_IDS = [case.name for case in CASES]


def dump(model: Any) -> Any:
    """What the command prints for a lookup's result."""
    if isinstance(model, list):
        return [dump(item) for item in model]
    if hasattr(model, "books") and hasattr(model, "complete"):
        # An author's books print as the list alone; whether it is whole goes
        # to standard error.
        return dump(model.books)
    return model.model_dump(mode="json", by_alias=True)


def unclocked(value: Any) -> Any:
    """The value with every updatedAt blanked. An author's profile carries the
    moment it was normalised, which differs between any two runs of the same
    question."""
    if isinstance(value, list):
        return [unclocked(item) for item in value]
    if isinstance(value, dict):
        return {
            key: None if key == "updatedAt" else unclocked(item)
            for key, item in value.items()
        }
    return value


def library_json(case: Case, region: str = "us") -> Any:
    """The lookup called directly, against a fresh stand-in."""
    return dump(asyncio.run(case.lookup(case.make_get(), region)))


def install_session(monkeypatch, get) -> None:
    """Replaces what the commands build their client with. The module object
    is patched, not a dotted name: they import it inside the handler, so the
    attribute is read on every run."""
    import libex_core.cli.session  # noqa: F401

    module = sys.modules["libex_core.cli.session"]

    @asynccontextmanager
    async def session(config):
        yield SimpleNamespace(get=get)

    monkeypatch.setattr(module, "client_session", session)
