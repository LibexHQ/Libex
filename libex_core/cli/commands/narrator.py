"""
`libex-core narrator`: books by narrator.
"""

import argparse

from libex_core.cli._args import (
    add_command,
    add_group,
    add_paging_options,
    add_region_option,
)


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    narrator = add_group(
        subparsers,
        "narrator",
        "look up books by narrator",
        "Audible exposes narrators by name only, so a narrator is looked up "
        "by name.",
    )
    books = add_command(
        narrator,
        "books",
        "fetch books by narrator",
        "Print the books a narrator has read as JSON. Exits 3 when nothing matched.",
    )
    books.add_argument("name", metavar="NAME", help="name of the narrator")
    add_paging_options(books)
    add_region_option(books)
    books.set_defaults(handler=run_books)


def run_books(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import narrator_books

    return run_lookup(
        lambda get, store: narrator_books(
            get, args.name, limit=args.limit, page=args.page, region=args.region,
            store=store
        )
    )
