"""
`libex-core search` and `libex-core quick-search`: find books.
"""

import argparse

from libex_core.cli._args import add_command, add_paging_options, add_region_option


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    search = add_command(
        subparsers,
        "search",
        "search the catalog",
        "Print the books matching the given fields as JSON. --query stands in "
        "for --title when no title is given. Exits 3 when nothing matched.",
    )
    for field, text in (
        ("title", "words in the title"),
        ("author", "name of the author"),
        ("narrator", "name of the narrator"),
        ("publisher", "name of the publisher"),
        ("keywords", "keywords to match anywhere"),
        ("query", "stands in for the title when none is given"),
    ):
        search.add_argument(f"--{field}", help=text)
    search.add_argument("--sort-by", help="the sort order, using the name Audible gives it")
    add_paging_options(search)
    add_region_option(search)
    search.set_defaults(handler=run_search)

    quick = add_command(
        subparsers,
        "quick-search",
        "search by keywords through suggestions",
        "Resolve keywords through Audible's search suggestions and print the "
        "full books they name as JSON. Exits 3 when nothing matched.",
    )
    quick.add_argument("keywords", metavar="KEYWORDS", help="the words to search for")
    add_region_option(quick)
    quick.set_defaults(handler=run_quick_search)


def run_search(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import search

    return run_lookup(
        lambda get: search(
            get,
            title=args.title,
            author=args.author,
            narrator=args.narrator,
            publisher=args.publisher,
            keywords=args.keywords,
            query=args.query,
            products_sort_by=args.sort_by,
            limit=args.limit,
            page=args.page,
            region=args.region,
        )
    )


def run_quick_search(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import quick_search

    return run_lookup(lambda get: quick_search(get, args.keywords, region=args.region))
