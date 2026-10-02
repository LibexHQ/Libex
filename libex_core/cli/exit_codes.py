"""
Process exit statuses, and the one place an exception becomes one.

The numbers are a public contract: a shell script branches on them, so none
is reassigned once published. 2 is argparse's own usage status, kept so a
mistyped flag and a rejected argument report the same way.
"""

import enum
from dataclasses import dataclass

from libex_core.cli.environment import ConfigError


class ExitCode(enum.IntEnum):
    OK = 0
    ERROR = 1
    USAGE = 2
    NOT_FOUND = 3
    UPSTREAM_UNAVAILABLE = 4
    CONFIG = 5
    INTERRUPTED = 130
    BROKEN_PIPE = 141


# Printed in the man page's EXIT STATUS section, in this order.
DESCRIPTIONS: dict[ExitCode, str] = {
    ExitCode.OK: "The command succeeded.",
    ExitCode.ERROR: "An unexpected error occurred. Rerun with -vv for a traceback.",
    ExitCode.USAGE: "The command line was not understood, or an argument was rejected.",
    ExitCode.NOT_FOUND: "The requested item does not exist.",
    ExitCode.UPSTREAM_UNAVAILABLE: "Audible could not be reached or did not answer. Retrying later may succeed.",
    ExitCode.CONFIG: "The environment configuration is missing or invalid.",
    ExitCode.INTERRUPTED: "Interrupted by SIGINT.",
    ExitCode.BROKEN_PIPE: "The reader of standard output went away before the output was complete.",
}


@dataclass(frozen=True)
class Failure:
    """What the error line says and which status the process ends with."""
    exit_code: ExitCode
    code: str
    message: str


# Exception class -> (status, short machine-readable code), first match wins
# via isinstance. Only classes whose message is fixed text written by this
# package belong here: the message is printed as-is. Failures that map over
# the library's error codes extend this table, keyed by class and reading
# the code off the instance, rather than growing a second dispatch.
_BY_EXCEPTION: tuple[tuple[type[Exception], ExitCode, str], ...] = (
    (ConfigError, ExitCode.CONFIG, "config_error"),
)


def classify(exc: Exception) -> Failure:
    for exc_type, exit_code, code in _BY_EXCEPTION:
        if isinstance(exc, exc_type):
            return Failure(exit_code, code, str(exc))
    # An unmapped exception's text is not printed: it can come from
    # anywhere under the transport, and the traceback at -vv is the
    # deliberate way to see it.
    return Failure(
        ExitCode.ERROR,
        "unexpected_error",
        f"unexpected {type(exc).__name__}; rerun with -vv for a traceback",
    )
