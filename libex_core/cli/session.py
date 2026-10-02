"""
Building a LibexClient and a LocalStore from the resolved configuration.
Imported lazily inside command handlers, so --help and completion never import
httpx, pydantic or the storage libraries.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from libex_core.audible.client import LibexClient
from libex_core.cli import store_state
from libex_core.cli.environment import (
    ALLOW_DIRECT_EGRESS_VARIABLE,
    PROXY_URL_VARIABLE,
    Config,
    ConfigError,
)


def build_client(config: Config) -> LibexClient:
    """Opens no connection. The client's own refusals are re-raised as
    ConfigError with fixed text, never chained, so the proxy URL it was
    given cannot ride along on an exception."""
    try:
        return LibexClient(
            proxy_url=config.proxy_url,
            allow_direct_egress=config.allow_direct_egress,
        )
    except ValueError:
        if config.proxy_url:
            raise ConfigError(
                f"{PROXY_URL_VARIABLE} is not a valid proxy URL: it needs an "
                "http or https scheme and a host (the port is optional)"
            ) from None
        raise ConfigError(
            f"no proxy is configured: set {PROXY_URL_VARIABLE}, or set "
            f"{ALLOW_DIRECT_EGRESS_VARIABLE}=1 to send requests from this "
            "machine's own address"
        ) from None


@asynccontextmanager
async def client_session(config: Config) -> AsyncIterator[LibexClient]:
    async with build_client(config) as client:
        yield client


def build_store(config: Config) -> Any:
    """A LocalStore for the configured target. Opens no connection. Raises
    ConfigError when storage is off, and the library's own fixed-text errors
    when the extra is missing or the target is not acceptable."""
    if config.storage is None:
        raise ConfigError(store_state.STORAGE_OFF)
    from libex_core.storage import LocalStore

    target = config.storage
    if target.path is not None:
        from sqlalchemy.engine import URL

        # A path is handed over as a URL's database part rather than spliced
        # into text, so a ? or # in a directory name cannot be read as syntax.
        return LocalStore(URL.create("sqlite+aiosqlite", database=target.path))
    return LocalStore(target.url)


_STATE_NAMES = {
    "empty": store_state.NOT_INITIALISED,
    "current": store_state.OK,
    "behind": store_state.OUTDATED,
    "ahead": store_state.AHEAD,
    "foreign": store_state.FOREIGN,
}


async def schema_state(store: Any) -> tuple[str, str | None]:
    """(state name, stored revision) as `db status` reports them."""
    state = await store.status()
    return _STATE_NAMES[state.state], state.revision


@asynccontextmanager
async def open_store(config: Config) -> AsyncIterator[Any]:
    """An opened store, closed afterwards. Never upgrades: a store that is not
    ready raises StoreNotReady naming the command that makes it so."""
    store = build_store(config)
    try:
        name, _ = await schema_state(store)
        if name != store_state.OK:
            raise store_state.StoreNotReady(store_state.MESSAGES[name])
        await store.open()
        yield store
    finally:
        await store.close()
