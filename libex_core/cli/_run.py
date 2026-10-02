"""
Running one lookup: build the client from the environment, make the call,
print the result. Imported lazily inside command handlers.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from libex_core.cli.environment import load_config
from libex_core.cli.exit_codes import ExitCode
from libex_core.cli.output import emit_json


async def _call(lookup: Callable[[Any], Awaitable[Any]]) -> Any:
    from libex_core.cli.session import client_session

    async with client_session(load_config()) as client:
        return await lookup(client.get)


def run_lookup(
    lookup: Callable[[Any], Awaitable[Any]],
    present: Callable[[Any], Any] | None = None,
) -> int:
    """A result that Audible answered only in part is still a result: it is
    printed whole and the status is 0, the partial fields being part of the
    shape. present turns the library's result into what is printed, and is
    where a notice about a partial one is given. Anything raised is left for
    main() to map to a status."""
    result = asyncio.run(_call(lookup))
    emit_json(present(result) if present is not None else result)
    return ExitCode.OK
