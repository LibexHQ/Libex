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
    get_book,
    get_books,
    get_chapters,
    get_series,
    get_series_books,
    narrator_books,
    quick_search,
    search,
)
from tests.libex_core.test_lookup import (
    _batch_get,
    _search_get,
    _series_get,
    _suggestion_get,
    _product,
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

CASE_IDS = [case.name for case in CASES]


def dump(model: Any) -> Any:
    """What the command prints for a lookup's result."""
    if isinstance(model, list):
        return [dump(item) for item in model]
    return model.model_dump(mode="json", by_alias=True)


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
