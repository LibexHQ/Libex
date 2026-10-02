"""
Shared pieces for the libex-core command line tests: where the committed data
files live, a walk over the finished parser, and the subprocess helpers that
run the program in an environment holding nothing but what a test names.
"""

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "libex-core-data"

# Every environment variable the command line or the library under it could
# consult. In-process runs clear all of them first so a developer's own proxy
# settings cannot change a result.
AMBIENT_VARIABLES = (
    "LIBEX_CORE_PROXY_URL",
    "LIBEX_CORE_ALLOW_DIRECT_EGRESS",
    "LIBEX_CORE_STORAGE",
    "AUDIBLE_PROXY_URL",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
    "NO_PROXY",
    "NO_COLOR",
    "FORCE_COLOR",
    "COLUMNS",
)


def walk_parsers(parser, path=()):
    """Every parser in the tree with the words that reach it, root first."""
    found = [(path, parser)]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                found.extend(walk_parsers(child, path + (name,)))
    return found


def walk_actions(parser):
    """Every (path, action) in the tree, subparser containers included."""
    return [
        (path, action) for path, node in walk_parsers(parser) for action in node._actions
    ]


def clean_env(**extra):
    """The whole environment for a subprocess: the repository on the import
    path and the named variables, nothing inherited."""
    return {"PYTHONPATH": str(REPO_ROOT), **extra}


def run_python(args, *, env, cwd, timeout=30, **kwargs):
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=timeout,
        **kwargs,
    )


# Run in a child with a throwaway `probe` command grafted onto the real
# parser, because no shipped command makes a request yet and the places a
# credential could surface are the failure paths of one that does. The child
# is a separate process on purpose: it needs real sockets, which the unit
# suite blocks in this one.
PROBE_SCRIPT = """
import argparse
import asyncio
import sys

import libex_core.cli.main
from libex_core.cli.environment import load_config
from libex_core.cli.session import client_session

module = sys.modules["libex_core.cli.main"]
real_build_parser = module.build_parser


async def fetch():
    async with client_session(load_config()) as client:
        await client.get("us", "/1.0/catalog/products/B000000000")


def build_parser():
    parser = real_build_parser()
    container = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    container.add_parser("probe").set_defaults(handler=lambda args: asyncio.run(fetch()))
    return parser


module.build_parser = build_parser
sys.exit(module.main(sys.argv[1:]))
"""
