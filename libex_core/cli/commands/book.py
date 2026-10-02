"""
`libex-core book`: one book, many books, or a book's chapters.
"""

import argparse
import re
import sys
from pathlib import Path

from libex_core.cli._args import add_command, add_group, add_region_option
from libex_core.cli._shaping import add_shaping_options, shaping_kwargs

# 1000 ASINs of 10 characters with separators is about 11 KB, so anything
# near this is not a list of ASINs.
_MAX_FILE_BYTES = 1 << 20
_SEPARATORS = re.compile(r"[,\s]+")


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    book = add_group(
        subparsers,
        "book",
        "look up books and chapters",
        "Look up one book, many books at once, or a book's chapters. Book "
        "ASINs belong to one marketplace, so use the --region the book "
        "was published in.",
    )

    get = add_command(
        book,
        "get",
        "fetch one book",
        "Print one book as JSON. Exits 3 when Audible has no such book.",
    )
    get.add_argument("asin", metavar="ASIN", help="ASIN of the book")
    add_region_option(get)
    get.set_defaults(handler=run_get)

    bulk = add_command(
        book,
        "bulk",
        "fetch up to 1000 books",
        "Print up to 1000 books as JSON, with the ASINs that were not found, "
        "were only placeholders, or could not be fetched listed beside them. "
        "ASINs may be given as arguments, in a file, or both, separated by "
        "commas or white space. The books can be filtered and sorted. A "
        "result that is only partly complete is still printed and the "
        "status is 0.",
    )
    bulk.add_argument(
        "asins", metavar="ASIN", nargs="*", help="ASIN of a book, repeat for more"
    )
    bulk.add_argument(
        "--file",
        metavar="PATH",
        help="read more ASINs from this file, or from standard input when it is -",
    )
    add_region_option(bulk)
    add_shaping_options(bulk, "asc")
    bulk.set_defaults(handler=run_bulk)

    chapters = add_command(
        book,
        "chapters",
        "fetch the chapter list of a book",
        "Print a book's chapters as JSON. Exits 3 when there is no chapter "
        "list, which is true of many records.",
    )
    chapters.add_argument("asin", metavar="ASIN", help="ASIN of the book")
    add_region_option(chapters)
    chapters.set_defaults(handler=run_chapters)


def run_get(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import get_book

    return run_lookup(lambda get: get_book(get, args.asin, region=args.region))


def run_chapters(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import get_chapters

    return run_lookup(lambda get: get_chapters(get, args.asin, region=args.region))


def _read_file(path: str) -> str:
    from libex_core.exceptions import ErrorCode, NotFoundException

    try:
        if path == "-":
            data = sys.stdin.buffer.read(_MAX_FILE_BYTES + 1)
        else:
            with Path(path).open("rb") as handle:
                data = handle.read(_MAX_FILE_BYTES + 1)
        if len(data) > _MAX_FILE_BYTES:
            raise ValueError
        return data.decode("utf-8")
    except (OSError, ValueError):
        # A fixed message: neither the path nor the reason is repeated.
        raise NotFoundException(
            "The ASIN list could not be read as text of a sensible size",
            code=ErrorCode.INVALID_REQUEST,
        ) from None


def split_asins(parts: list[str]) -> list[str]:
    return [item for part in parts for item in _SEPARATORS.split(part) if item]


def run_bulk(args: argparse.Namespace) -> int:
    from libex_core.cli._run import run_lookup
    from libex_core.lookup import get_books

    parts = list(args.asins)
    if args.file is not None:
        parts.append(_read_file(args.file))
    asins = split_asins(parts)
    return run_lookup(
        lambda get: get_books(
            get, asins, region=args.region, **shaping_kwargs(args)
        )
    )
