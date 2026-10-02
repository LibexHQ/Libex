"""
Process exit statuses, and the one place an exception becomes one.

The numbers are a public contract: a shell script branches on them, so none
is reassigned once published. 2 is argparse's own usage status, kept so a
mistyped flag and a rejected argument report the same way.

A LibexException maps by its `code`, so the status says whose gap the failure
is rather than which class raised it. The error line's `code` suffix is that
same value for those failures. `config_error` and `unexpected_error` are this
tool's own codes, not members of the ErrorCode vocabulary: the library never
raises them and an embedder cannot receive them from it.
"""

import enum
import sys
from dataclasses import dataclass

from libex_core.cli.environment import ConfigError
from libex_core.cli.store_state import StoreNotReady
from libex_core.exceptions import ErrorCode, LibexException
from libex_core.storage import StorageUnavailable


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
    ExitCode.CONFIG: "The environment configuration is missing or invalid, or the local store is off, missing its extra, unreachable or not ready. The error line names this as config_error or store_error, codes of this tool that the library itself never reports.",
    ExitCode.INTERRUPTED: "Interrupted by SIGINT.",
    ExitCode.BROKEN_PIPE: "The reader of standard output went away before the output was complete.",
}


@dataclass(frozen=True)
class Failure:
    """What the error line says and which status the process ends with."""
    exit_code: ExitCode
    code: str
    message: str


# ErrorCode -> status. A code with no entry here (the vocabulary is additive)
# falls through to ERROR rather than being guessed at.
_BY_ERROR_CODE: dict[ErrorCode, ExitCode] = {
    ErrorCode.INVALID_REQUEST: ExitCode.USAGE,
    ErrorCode.NOT_ON_AUDIBLE: ExitCode.NOT_FOUND,
    ErrorCode.NOT_IN_LIBEX: ExitCode.NOT_FOUND,
    ErrorCode.WITHHELD: ExitCode.NOT_FOUND,
    ErrorCode.UPSTREAM_UNAVAILABLE: ExitCode.UPSTREAM_UNAVAILABLE,
}


def classify(exc: Exception) -> Failure:
    # Every branch prints the message as-is, so both are limited to classes
    # whose text is fixed by this package or by libex_core.
    if isinstance(exc, ConfigError):
        return Failure(ExitCode.CONFIG, "config_error", str(exc))
    if isinstance(exc, StoreNotReady):
        return Failure(ExitCode.CONFIG, "store_error", str(exc))
    if isinstance(exc, StorageUnavailable):
        return Failure(ExitCode.CONFIG, "config_error", str(exc))
    store = sys.modules.get("libex_core.storage.store")
    if store is not None and isinstance(exc, store.StoreError):
        # The store's own messages name what is wrong and never quote the
        # URL, which is the one thing that must not reach this line.
        code = "config_error" if isinstance(exc, store.StoreConfigError) else "store_error"
        return Failure(ExitCode.CONFIG, code, str(exc))
    if isinstance(exc, LibexException):
        return Failure(
            _BY_ERROR_CODE.get(exc.code, ExitCode.ERROR),
            str(exc.code.value),
            exc.message,
        )
    # An unmapped exception's text is not printed: it can come from
    # anywhere under the transport, and the traceback at -vv is the
    # deliberate way to see it.
    return Failure(
        ExitCode.ERROR,
        "unexpected_error",
        f"unexpected {type(exc).__name__}; rerun with -vv for a traceback",
    )
