"""
argparse construction shared by the root parser and every command's parser.
"""

import argparse
import sys
from collections.abc import Callable
from typing import Any


class Parser(argparse.ArgumentParser):
    """An ArgumentParser whose refusals never repeat what was typed.

    argparse quotes the offending value for a bad choice and lists every
    argument it did not recognise, which would put a mistyped secret on the
    screen and into any log of it. The messages here are fixed text. Command
    parsers are built from this class too, because add_subparsers builds its
    children from the type of the parser it hangs off."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Flags are an exact contract: no abbreviations, so no ambiguity
        # error that would repeat what was typed.
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def _check_value(self, action: argparse.Action, value: Any) -> None:
        if action.choices is not None and value not in action.choices:
            options = ", ".join(str(choice) for choice in action.choices)
            raise argparse.ArgumentError(
                action, f"invalid choice (choose from {options})"
            )

    def parse_args(self, args: Any = None, namespace: Any = None) -> Any:
        parsed, extras = self.parse_known_args(args, namespace)
        if extras:
            self.error("unrecognized arguments")
        return parsed


def parser_options() -> dict[str, Any]:
    """Python 3.14 colors argparse output on a terminal unless told not to."""
    if sys.version_info >= (3, 14):
        return {"color": False}
    return {}


def add_help_option(parser: argparse.ArgumentParser) -> None:
    # Added by hand rather than by add_help=True so the help text is a
    # string written here, not argparse's translated default; the man page
    # and the completion scripts are generated from these strings and must
    # not vary with the reader's locale.
    parser.add_argument(
        "-h", "--help", action="help", help="show this help message and exit"
    )


# Spelled out here rather than read from the region enum so that building the
# parser, which --help and completion do, imports nothing heavy. A test holds
# this equal to the library's own list.
REGIONS = ("us", "uk", "ca", "au", "de", "fr", "it", "es", "jp", "in", "br")


def add_region_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--region",
        choices=REGIONS,
        default="us",
        help="the Audible marketplace to ask, default us",
    )


def bounded_int(low: int, high: int) -> Callable[[str], int]:
    """An argparse type for a whole number in low..high. The message is fixed
    so a rejected value is never echoed back."""

    def convert(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            value = low - 1
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(
                f"must be a whole number from {low} to {high}"
            )
        return value

    convert.__name__ = "number"
    return convert


def add_paging_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--limit",
        type=bounded_int(1, 50),
        default=10,
        help="results per page, 1 to 50, default 10",
    )
    parser.add_argument(
        "--page",
        type=bounded_int(0, 9),
        default=0,
        help="page of results to return, counting from 0 to 9, default 0",
    )


def add_group(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    name: str,
    summary: str,
    description: str,
) -> "argparse._SubParsersAction[argparse.ArgumentParser]":
    """A command that only holds further commands."""
    group = subparsers.add_parser(
        name,
        add_help=False,
        help=summary,
        description=description,
        **parser_options(),
    )
    add_help_option(group)
    return group.add_subparsers(
        dest=f"{name}_command", metavar="COMMAND", required=True
    )


def add_command(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    name: str,
    summary: str,
    description: str,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        name,
        add_help=False,
        help=summary,
        description=description,
        **parser_options(),
    )
    add_help_option(parser)
    return parser
