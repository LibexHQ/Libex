"""
Building a LibexClient from the resolved configuration. Imported only by the
commands that talk to Audible, so --help and completion never pay for httpx
and pydantic.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from libex_core.audible.client import LibexClient
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
