"""
`libex-core abs`: searches answered in the Audiobookshelf
custom-metadata-provider shape.
"""

import argparse

from libex_core.cli._args import add_command, add_group, add_region_option


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    abs_group = add_group(
        subparsers,
        "abs",
        "search in the Audiobookshelf provider shape",
        "Searches that print their matches in the Audiobookshelf "
        "custom-metadata-provider shape, at most five.",
    )

    search = add_command(
        abs_group,
        "search",
        "search the catalog",
        "Print up to five matches as JSON. --query stands in for --title.",
    )
    for field, text in (
        ("title", "words in the title"),
        ("query", "stands in for the title"),
        ("author", "name of the author"),
        ("keywords", "keywords to match anywhere"),
    ):
        search.add_argument(f"--{field}", help=text)
    add_region_option(search)
    search.set_defaults(handler=run_search)

    quick = add_command(
        abs_group,
        "quick-search",
        "search through suggestions",
        "Print up to five matches as JSON. The first of --keywords, --query "
        "and --title that is given is searched, and giving none is an error.",
    )
    for field, text in (
        ("keywords", "the words to search for"),
        ("query", "the words to search for when no keywords are given"),
        ("title", "the words to search for when neither is given"),
    ):
        quick.add_argument(f"--{field}", help=text)
    add_region_option(quick)
    quick.set_defaults(handler=run_quick_search)


def run_search(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import abs_search

    return run_lookup(
        lambda get, store: abs_search(
            get,
            title=args.title,
            query=args.query,
            author=args.author,
            keywords=args.keywords,
            region=args.region,
            store=store,
        )
    )


def run_quick_search(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import abs_quick_search

    return run_lookup(
        lambda get, store: abs_quick_search(
            get,
            keywords=args.keywords,
            query=args.query,
            title=args.title,
            region=args.region,
            store=store,
        )
    )
