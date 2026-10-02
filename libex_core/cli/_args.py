"""
argparse construction shared by the root parser and every command's parser.
"""

import argparse
import sys
from typing import Any


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
