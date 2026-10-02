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


def run_lookup(lookup: Callable[[Any], Awaitable[Any]]) -> int:
    """A result that Audible answered only in part is still a result: it is
    printed whole and the status is 0, the partial fields being part of the
    shape. Anything raised is left for main() to map to a status."""
    emit_json(asyncio.run(_call(lookup)))
    return ExitCode.OK
