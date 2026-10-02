"""
The filter, sort and paging options of the commands that read the local store,
which mirror the hosted /db routes. Spelled out here, not read from the
storage package, so building the parser imports nothing heavy; a test holds
every table below equal to what the readers accept.
"""

import argparse
from typing import Any

from libex_core.cli._args import REGIONS, bounded_int
from libex_core.cli._shaping import _BOOLEANS, _MAX_MINUTES, _dest, _flag, _number
from libex_core.shaping import BOOK_SORT_FIELDS

# name, type, help text. The hosted /db routes' book filters, in their order.
BOOK_FILTERS: tuple[tuple[str, type, str], ...] = (
    ("title", str, "only books whose title contains this text"),
    ("subtitle", str, "only books whose subtitle contains this text"),
    ("region", str, "only books of this marketplace"),
    ("description", str, "only books whose description contains this text"),
    ("summary", str, "only books whose summary contains this text"),
    ("publisher", str, "only books whose publisher contains this text"),
    ("copyright", str, "only books whose copyright line contains this text"),
    ("isbn", str, "only the book with this ISBN"),
    ("author_name", str, "only books by an author whose name contains this text"),
    ("series_name", str, "only books in a series whose name contains this text"),
    ("language", str, "only books in this language"),
    ("rating_better_than", float, "only books rated at least this"),
    ("rating_worse_than", float, "only books rated at most this"),
    ("longer_than", int, "only books at least this many minutes long"),
    ("shorter_than", int, "only books at most this many minutes long"),
    ("explicit", bool, "only books that are explicit (true) or are not (false)"),
    ("whisper_sync", bool, "only books with Whispersync (true) or without (false)"),
    ("has_pdf", bool, "only books with a PDF companion (true) or without (false)"),
    ("book_format", str, "only books of this format"),
    ("content_type", str, "only books of this content type"),
    ("content_delivery_type", str, "only books of this content delivery type"),
    ("is_listenable", bool, "only books that are listenable (true) or are not (false)"),
    ("is_buyable", bool, "only books that are buyable (true) or are not (false)"),
    ("is_vvab", bool, "only virtual voice audiobooks (true) or none of them (false)"),
    ("plan_name", str, "only books available under this Audible plan"),
    ("genre", str, "only books with a genre or tag whose name contains this text, db genres lists them"),
    ("category", str, "only books in this exact category id, or any of several separated by commas"),
)

NARRATOR_SORT_FIELDS = ("name", "source", "sourceUpdatedAt", "updatedAt")
# The buckets the hosted route takes, under names that are one word each so
# they can be offered by shell completion. The stored text is on the right.
AUDIOBOOKS_PRODUCED = {
    "1-10": "1 to 10",
    "11-20": "11 to 20",
    "21-50": "21 to 50",
    "51-100": "51 to 100",
    "more-than-100": "More than 100",
    "none-yet": "None yet",
}


def add_book_filters(parser: argparse.ArgumentParser, exclude: frozenset[str] = frozenset()) -> None:
    """One flag per book filter the matching hosted route takes. exclude names
    those that are the command's scope rather than a filter."""
    for name, kind, description in BOOK_FILTERS:
        if name in exclude:
            continue
        options: dict[str, Any] = {}
        if kind is bool:
            options["choices"] = _BOOLEANS
        elif kind is float:
            options["type"] = _number
        elif kind is int:
            options["type"] = bounded_int(0, _MAX_MINUTES)
        elif name == "region":
            options["choices"] = REGIONS
        parser.add_argument(
            _flag(name),
            dest=_dest(name),
            metavar=name.upper(),
            help=description,
            **options,
        )


def book_filter_kwargs(args: argparse.Namespace, exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    """The filters given, as the keywords the readers take."""
    given: dict[str, Any] = {}
    for name, kind, _ in BOOK_FILTERS:
        if name in exclude:
            continue
        value = getattr(args, _dest(name))
        if value is None:
            continue
        given[name] = value == "true" if kind is bool else value
    return given


def has_book_filter(args: argparse.Namespace) -> bool:
    return any(getattr(args, _dest(name)) is not None for name, _, _ in BOOK_FILTERS)


def add_book_sort(parser: argparse.ArgumentParser, default_order: str = "asc", note: str = "") -> None:
    parser.add_argument(
        "--sort",
        choices=BOOK_SORT_FIELDS,
        help=f"sort the books by this field{note}",
    )
    parser.add_argument(
        "--order",
        choices=("asc", "desc"),
        default=default_order,
        help=f"the direction of --sort, default {default_order}",
    )


def add_db_paging(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--limit",
        type=bounded_int(1, 100),
        default=20,
        help="results per page, 1 to 100, default 20",
    )
    parser.add_argument(
        "--page",
        type=bounded_int(1, 1_000_000),
        default=1,
        help="page of results to return, counting from 1, default 1",
    )
