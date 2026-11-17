"""
The exit probe and the app probe.

Exit probe invariants:
  - A fresh LibexClient per cycle, closed after, built with the stack's proxy
    and never with allow_direct_egress: the probe travels the exit it judges,
    and a wedged pool in the watcher cannot outlive a cycle.
  - Only a refusal or a dead path counts as a block: upstream 403 or 429, or
    no upstream status with a timeout, proxy or connect error behind it. A
    5xx, a 404 or a 200 that is not JSON says nothing about the exit and never
    leads to a rotation.
  - A cycle is blocked only when every probed region is block-type.
"""

# Standard library
from dataclasses import dataclass
from typing import Literal

# Third party
import httpx

# Internal
from app.core.logging import get_logger
from libex_core.audible.books import fetch_products
from libex_core.audible.client import LibexClient
from libex_core.exceptions import AudibleAPIException

logger = get_logger()

PROBE_ASIN = "0008433844"
PROBE_REGIONS = ("us", "uk")
_BLOCK_STATUSES = frozenset({403, 429})
_BLOCK_CAUSES = (httpx.TimeoutException, httpx.ProxyError, httpx.ConnectError)

Verdict = Literal["ok", "block", "other"]


def classify(exc: BaseException) -> Verdict:
    """Block-type or not, from upstream_status and the implicit exception chain."""
    if not isinstance(exc, AudibleAPIException):
        return "other"
    if exc.upstream_status is not None:
        return "block" if exc.upstream_status in _BLOCK_STATUSES else "other"
    return "block" if isinstance(exc.__context__, _BLOCK_CAUSES) else "other"


@dataclass(frozen=True)
class ExitProbe:
    verdicts: dict[str, Verdict]

    @property
    def blocked(self) -> bool:
        return all(v == "block" for v in self.verdicts.values())


async def probe_exit(proxy_url: str) -> ExitProbe:
    verdicts: dict[str, Verdict] = {}
    client = LibexClient(proxy_url=proxy_url)
    try:
        for region in PROBE_REGIONS:
            try:
                await fetch_products(client.get, [PROBE_ASIN], region)
                verdicts[region] = "ok"
            except Exception as exc:
                verdicts[region] = classify(exc)
                logger.info(
                    "Watch: exit probe failed",
                    extra={"region": region, "verdict": verdicts[region], "error_type": type(exc).__name__},
                )
    finally:
        await client.aclose()
    return ExitProbe(verdicts)


async def probe_app(base_url: str) -> bool:
    """
    A real /search against this stack's own API, direct and unauthenticated.
    True when either region answers 2xx; failing needs both to fail.
    """
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(45.0, connect=5.0),
        follow_redirects=False,
        trust_env=False,
    ) as http:
        for region in PROBE_REGIONS:
            try:
                response = await http.get(
                    "/search", params={"title": "the hobbit", "region": region, "limit": 1}
                )
                if response.is_success:
                    return True
            except Exception as exc:
                logger.info(
                    "Watch: app probe failed",
                    extra={"region": region, "error_type": type(exc).__name__},
                )
    return False
