"""
The filter and sort options of every command that lists books, built from the
library's own field specs so the command line cannot drift from what the
functions apply.
"""

import argparse
import math
import re
from typing import Any

from libex_core.cli._args import bounded_int
from libex_core.shaping import BOOK_FILTER_SPECS, BOOK_SORT_FIELDS

_BOOLEANS = ("true", "false")

# Help and completion text may hold only these characters, so a library
# description with a quote or a colon in it is reduced to the rest.
_UNSAFE_HELP = re.compile(r"[^A-Za-z0-9 ,.()/_-]")

# The largest length filter accepted, in minutes; far past any audiobook.
_MAX_MINUTES = 1_000_000


def _number(text: str) -> float:
    """A finite number. The message is fixed so a rejected value is never
    echoed back."""
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("must be a number")
    return value


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def _dest(name: str) -> str:
    return f"filter_{name}"


def add_shaping_options(parser: argparse.ArgumentParser, default_order: str) -> None:
    """One flag per filter in BOOK_FILTER_SPECS, then --sort and --order.
    default_order is the order the matching hosted route applies."""
    for spec in BOOK_FILTER_SPECS:
        options: dict[str, Any] = {}
        if spec.type is bool:
            options["choices"] = _BOOLEANS
        elif spec.type is float:
            options["type"] = _number
        elif spec.type is int:
            options["type"] = bounded_int(0, _MAX_MINUTES)
        parser.add_argument(
            _flag(spec.name),
            dest=_dest(spec.name),
            metavar=spec.name.upper(),
            help=_UNSAFE_HELP.sub("", spec.description),
            **options,
        )
    parser.add_argument(
        "--sort",
        choices=BOOK_SORT_FIELDS,
        help="sort the books by this field, default is the order they were found in",
    )
    parser.add_argument(
        "--order",
        choices=("asc", "desc"),
        default=default_order,
        help=f"the direction of --sort, default {default_order}",
    )


def shaping_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """The filters, sort and order keywords the library's lookups take."""
    filters: dict[str, Any] = {}
    for spec in BOOK_FILTER_SPECS:
        value = getattr(args, _dest(spec.name))
        if value is None:
            continue
        filters[spec.name] = value == "true" if spec.type is bool else value
    return {
        "filters": filters or None,
        "sort": args.sort,
        "order": args.order,
    }
