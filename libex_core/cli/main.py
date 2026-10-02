"""
The `libex-core` entry point.
"""

import logging
import os
import sys

from libex_core.cli.exit_codes import ExitCode, classify
from libex_core.cli.output import (
    LOGGER_NAME,
    attach_stderr_logging,
    detach_stderr_logging,
    error_line,
)
from libex_core.cli.parser import build_parser


def _log_level(quiet: bool, verbose: int) -> int:
    if quiet:
        return logging.CRITICAL
    if verbose >= 2:
        return logging.DEBUG
    if verbose == 1:
        return logging.INFO
    return logging.WARNING


def _run(argv: list[str] | None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse's own exits: 0 after --help or --version, 2 on bad usage.
        return exc.code if isinstance(exc.code, int) else ExitCode.OK
    attach_stderr_logging(_log_level(args.quiet, args.verbose))
    try:
        return int(args.handler(args))
    except BrokenPipeError:
        raise
    except Exception as exc:
        failure = classify(exc)
        # The stdlib renders the traceback; nothing walks the exception
        # chain or its frames, so a value that was deliberately kept out of
        # a message does not come back through here.
        logging.getLogger(LOGGER_NAME).debug("command failed", exc_info=exc)
        print(error_line(failure.message, failure.code), file=sys.stderr)
        return failure.exit_code
    finally:
        detach_stderr_logging()


def main(argv: list[str] | None = None) -> int:
    try:
        code = _run(argv)
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        # The reader closed early. Point stdout at devnull so the flush the
        # interpreter does at exit cannot raise a second time.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
        except (OSError, ValueError):
            pass
        return ExitCode.BROKEN_PIPE
    except KeyboardInterrupt:
        return ExitCode.INTERRUPTED
