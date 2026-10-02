"""
`libex-core author`: an author's profile, a search by name, and their books.
"""

import argparse
import logging
from typing import Any

from libex_core.cli._args import add_command, add_group, add_region_option
from libex_core.cli._shaping import add_shaping_options, shaping_kwargs

logger = logging.getLogger("libex")


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    author = add_group(
        subparsers,
        "author",
        "look up an author",
        "Look up an author by ASIN or by name, or the books they are credited "
        "with. Author ASINs are the same in every marketplace but each "
        "marketplace lists its own books, so use the --region you want.",
    )

    get = add_command(
        author,
        "get",
        "fetch an author profile",
        "Print the author's profile as JSON. Exits 3 when Audible has no "
        "such author.",
    )
    get.add_argument("asin", metavar="ASIN", help="ASIN of the author")
    add_region_option(get)
    get.set_defaults(handler=run_get)

    find = add_command(
        author,
        "search",
        "search for authors by name",
        "Print the authors Audible suggests for the name, as JSON. Exits 3 "
        "when no author was found.",
    )
    find.add_argument("name", metavar="NAME", help="name of the author")
    add_region_option(find)
    find.set_defaults(handler=run_search)

    books = add_command(
        author,
        "books",
        "fetch the books of an author by ASIN",
        "Print the full books an author is credited with as JSON, newest "
        "first unless sorted. The books can be filtered and sorted. When the "
        "list may not be whole, it is still printed, a warning naming the "
        "reasons goes to standard error, and the status is 0.",
    )
    books.add_argument("asin", metavar="ASIN", help="ASIN of the author")
    add_region_option(books)
    add_shaping_options(books, "asc")
    books.set_defaults(handler=run_books)

    by_name = add_command(
        author,
        "books-by-name",
        "fetch the books of an author by exact name",
        "Print the full books whose author name matches exactly, ignoring "
        "case, as JSON. For when the ASIN is not known. The books can be "
        "filtered and sorted. When the list may not be whole, it is still "
        "printed, a warning naming the reasons goes to standard error, and "
        "the status is 0. Exits 3 when no book matched.",
    )
    by_name.add_argument("name", metavar="NAME", help="exact name of the author")
    add_region_option(by_name)
    add_shaping_options(by_name, "asc")
    by_name.set_defaults(handler=run_books_by_name)


def run_get(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import get_author

    return run_lookup(lambda get, store: get_author(get, args.asin, region=args.region, store=store))


def run_search(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import search_authors

    return run_lookup(lambda get, store: search_authors(get, args.name, region=args.region, store=store))


def _books_only(result: Any) -> Any:
    """The hosted route's body is the list of books alone, with whether it is
    whole carried beside it. Here that is a warning on standard error. The
    reasons are words from a fixed list, so the notice holds no value
    the caller or Audible supplied."""
    if not result.complete:
        logger.warning(
            "the list of books may be incomplete (%s)",
            ", ".join(result.incomplete_reasons),
        )
    return result.books


def run_books(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import get_author_books

    return run_lookup(
        lambda get, store: get_author_books(
            get, args.asin, region=args.region, **shaping_kwargs(args),
            store=store
        ),
        _books_only,
    )


def run_books_by_name(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import get_author_books_by_name

    return run_lookup(
        lambda get, store: get_author_books_by_name(
            get, args.name, region=args.region, **shaping_kwargs(args),
            store=store
        ),
        _books_only,
    )
