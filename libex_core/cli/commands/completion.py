"""
`libex-core completion SHELL`: print a static completion script.
"""

import argparse

from libex_core.cli._args import add_help_option, parser_options
from libex_core.cli.exit_codes import ExitCode
from libex_core.cli.output import emit_text

SHELLS = ("bash", "zsh", "fish")


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "completion",
        add_help=False,
        help="print a shell completion script",
        description=(
            "Print the completion script for the named shell to standard "
            "output. Source it, or install it where the shell looks for "
            "completions."
        ),
        **parser_options(),
    )
    add_help_option(parser)
    parser.add_argument(
        "shell", choices=SHELLS, help="the shell to print the script for"
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    # Imported here because the renderer walks the finished parser, which
    # itself imports this module.
    from libex_core.cli import _render
    from libex_core.cli.parser import build_parser

    emit_text(_render.completion_script(build_parser(), args.shell))
    return ExitCode.OK
