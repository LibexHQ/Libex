"""
The command tree. Built by a function, never at import, and the program name
is fixed so `python -m libex_core` and the installed script show the same
usage text and the generated man page and completions match both.
"""

from libex_core import __version__
from libex_core.cli._args import Parser, add_help_option, parser_options
from libex_core.cli.commands import (
    abs_search,
    author,
    book,
    completion,
    config,
    db,
    narrator,
    releases,
    search,
    series,
)
from libex_core.cli.environment import (
    ALLOW_DIRECT_EGRESS_VARIABLE,
    PROXY_URL_VARIABLE,
    STORAGE_VARIABLE,
)

PROG = "libex-core"

# Fixed on purpose: commands are never discovered at run time.
COMMANDS = (
    book,
    series,
    author,
    search,
    abs_search,
    narrator,
    releases,
    db,
    completion,
    config,
)


def build_parser() -> Parser:
    parser = Parser(
        prog=PROG,
        add_help=False,
        description=(
            "Fetch Audible metadata. Results are written to standard output "
            "as JSON, and logs and errors go to standard error."
        ),
        epilog=(
            f"Environment: {PROXY_URL_VARIABLE} sets the proxy requests go "
            f"through, and {ALLOW_DIRECT_EGRESS_VARIABLE}=1 permits sending "
            "them from this machine's own address when there is none. "
            f"{STORAGE_VARIABLE} turns on the local store that the db "
            "commands read and lookups keep their results in."
        ),
        **parser_options(),
    )
    add_help_option(parser)
    parser.add_argument(
        "--version",
        action="version",
        version=f"{PROG} {__version__}",
        help="show the version number and exit",
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="print only the final error line",
    )
    verbosity.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help=(
            "more detail on standard error, "
            "repeat as -vv for debug output with tracebacks"
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command", metavar="COMMAND", required=True
    )
    for command in COMMANDS:
        command.register(subparsers)
    return parser
