"""
Running one command's work: build what it needs from the environment, make the
call, print the result. Imported lazily inside command handlers.
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

from libex_core.cli.environment import load_config
from libex_core.cli.exit_codes import ExitCode
from libex_core.cli.output import emit_json


async def _call(lookup: Callable[[Any, Any], Awaitable[Any]]) -> Any:
    from libex_core.cli.session import client_session, open_store

    config = load_config()
    async with AsyncExitStack() as stack:
        # Opened first, so a store that is not ready stops the command before
        # a request is made rather than after one whose answer would not be
        # kept.
        store = None
        if config.storage is not None:
            store = await stack.enter_async_context(open_store(config))
        client = await stack.enter_async_context(client_session(config))
        return await lookup(client.get, store)


def run_lookup(
    lookup: Callable[[Any, Any], Awaitable[Any]],
    present: Callable[[Any], Any] | None = None,
) -> int:
    """lookup is given the request callable and the open store, or None when
    storage is off. A result that Audible answered only in part is still a
    result: it is printed whole and the status is 0, the partial fields being
    part of the shape. present turns the library's result into what is
    printed, and is where a notice about a partial one is given. Anything
    raised is left for main() to map to a status."""
    result = asyncio.run(_call(lookup))
    emit_json(present(result) if present is not None else result)
    return ExitCode.OK


async def _read(read: Callable[[Any], Awaitable[Any]]) -> Any:
    from libex_core.cli.session import open_store

    async with open_store(load_config()) as store:
        async with store.session() as session:
            return await read(session)


def run_stored(read: Callable[[Any], Awaitable[Any]]) -> int:
    """Reads from the local store only; no request is made and no proxy is
    needed. read is given a session and returns what is printed."""
    emit_json(asyncio.run(_read(read)))
    return ExitCode.OK
