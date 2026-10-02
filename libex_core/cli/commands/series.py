"""
`libex-core series`: a series' own record, and its books.
"""

import argparse

from libex_core.cli._args import add_command, add_group, add_region_option


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    series = add_group(
        subparsers,
        "series",
        "look up a series",
        "Look up a series by its ASIN, or the books in it.",
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
        "Print the full books in a series as JSON, in series order. A member "
        "that cannot be resolved is left out.",
    )
    books.add_argument("asin", metavar="ASIN", help="ASIN of the series")
    add_region_option(books)
    books.set_defaults(handler=run_books)


def run_get(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import get_series

    return run_lookup(lambda get: get_series(get, args.asin, region=args.region))


def run_books(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import get_series_books

    return run_lookup(lambda get: get_series_books(get, args.asin, region=args.region))
