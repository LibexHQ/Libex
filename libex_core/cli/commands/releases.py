"""
`libex-core releases`: new releases, titles coming soon, and the categories
that scope both.
"""

import argparse
import re

from libex_core.cli._args import (
    add_command,
    add_group,
    add_region_option,
    bounded_int,
)
from libex_core.cli._shaping import add_shaping_options, shaping_kwargs

# Spelled out here so building the parser imports nothing heavy; a test holds
# it equal to the library's own RELEASE_WINDOWS.
WINDOWS = ("30", "60", "90", "120", "240", "365")

_CATEGORY_ID = re.compile(r"\d{1,12}")


def category_id(text: str) -> str:
    """An Audible category id. The message is fixed so a rejected value is
    never echoed back."""
    if not _CATEGORY_ID.fullmatch(text):
        raise argparse.ArgumentTypeError("must be a numeric category id")
    return text


def _add_window_options(parser: argparse.ArgumentParser, word: str) -> None:
    parser.add_argument(
        "--days",
        choices=WINDOWS,
        default="30",
        help=f"how many days {word}, default 30",
    )
    parser.add_argument(
        "--category",
        type=category_id,
        metavar="ID",
        help=(
            "only this category, by the id that releases categories prints. "
            "Without one the scan is a live sample, not the whole catalog"
        ),
    )


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    releases = add_group(
        subparsers,
        "releases",
        "look up new and upcoming releases",
        "Books released recently or about to be, scanned live from Audible, "
        "and the categories that narrow the scan.",
    )

    new = add_command(
        releases,
        "new",
        "books released recently",
        "Print the books released in the last --days as JSON, newest first "
        "unless sorted. Pre-orders are left out. The books can be filtered "
        "and sorted. Exits 3 when none are left.",
    )
    _add_window_options(new, "back to look")
    add_region_option(new)
    add_shaping_options(new, "desc")
    new.set_defaults(handler=run_new)

    soon = add_command(
        releases,
        "coming-soon",
        "books about to be released",
        "Print the books releasing in the next --days as JSON, soonest first "
        "unless sorted. Titles already out are left out. The books can be "
        "filtered and sorted. Exits 3 when none are left.",
    )
    _add_window_options(soon, "ahead to look")
    add_region_option(soon)
    add_shaping_options(soon, "asc")
    soon.set_defaults(handler=run_coming_soon)

    categories = add_command(
        releases,
        "categories",
        "the genre categories of a marketplace",
        "Print Audible's genre categories as JSON, a tree by default. The ids "
        "are the values --category takes.",
    )
    categories.add_argument(
        "--flat",
        action="store_true",
        help="a flat list, each node carrying its ancestors, instead of a tree",
    )
    categories.add_argument(
        "--depth",
        type=bounded_int(1, 9),
        help="how many levels to return, 1 is the top level only, default all",
    )
    add_region_option(categories)
    categories.set_defaults(handler=run_categories)


def run_new(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import new_releases

    return run_lookup(
        lambda get: new_releases(
            get,
            int(args.days),
            args.category,
            region=args.region,
            **shaping_kwargs(args),
        )
    )


def run_coming_soon(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import coming_soon

    return run_lookup(
        lambda get: coming_soon(
            get,
            int(args.days),
            args.category,
            region=args.region,
            **shaping_kwargs(args),
        )
    )


def run_categories(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import categories

    return run_lookup(
        lambda get: categories(
            get, region=args.region, flat=args.flat, depth=args.depth
        )
    )
