"""
`libex-core series`: a series' own record, and its books.
"""

import argparse

from libex_core.cli._args import add_command, add_group, add_region_option
from libex_core.cli._shaping import add_shaping_options, shaping_kwargs


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    series = add_group(
        subparsers,
        "series",
        "look up a series",
        "Look up a series by its ASIN or by name, or the books in it.",
    )

    get = add_command(
        series,
        "get",
        "fetch one series record",
        "Print the series record as JSON. Exits 3 when Audible has no such series.",
    )
    get.add_argument("asin", metavar="ASIN", help="ASIN of the series")
    add_region_option(get)
    get.set_defaults(handler=run_get)

    books = add_command(
        series,
        "books",
        "fetch the books in a series",
        "Print the full books in a series as JSON, in series order unless "
        "sorted. A member that cannot be resolved is left out. The books can "
        "be filtered and sorted.",
    )
    books.add_argument("asin", metavar="ASIN", help="ASIN of the series")
    add_region_option(books)
    add_shaping_options(books, "asc")
    books.set_defaults(handler=run_books)

    find = add_command(
        series,
        "search",
        "search for series by name",
        "Print the series whose books are titled with the name, as JSON. "
        "Exits 3 when no series was found.",
    )
    find.add_argument("name", metavar="NAME", help="name of the series")
    add_region_option(find)
    find.set_defaults(handler=run_search)


def run_get(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import get_series

    return run_lookup(lambda get, store: get_series(get, args.asin, region=args.region, store=store))


def run_books(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import get_series_books

    return run_lookup(
        lambda get, store: get_series_books(
            get, args.asin, region=args.region, **shaping_kwargs(args),
            store=store
        )
    )


def run_search(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import search_series

    return run_lookup(lambda get, store: search_series(get, args.name, region=args.region, store=store))
