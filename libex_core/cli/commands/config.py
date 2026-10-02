"""
`libex-core config`: show how requests would leave this machine.
"""

import argparse

from libex_core.cli._args import add_help_option, parser_options
from libex_core.cli.environment import load_config
from libex_core.cli.exit_codes import ExitCode
from libex_core.cli.output import emit_json


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "config",
        add_help=False,
        help="show how requests would leave this machine",
        description=(
            "Print, as JSON, whether requests go through a proxy or leave "
            "directly, and the proxy host. The proxy URL itself is never "
            "printed. No request is made."
        ),
        **parser_options(),
    )
    add_help_option(parser)
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    from libex_core.cli.session import build_client

    summary = build_client(load_config()).transport_summary()
    emit_json({"transport": {"mode": summary.mode, "host": summary.host}})
    return ExitCode.OK
