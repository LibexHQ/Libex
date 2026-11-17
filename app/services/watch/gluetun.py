"""
Client for one stack's gluetun control server, and the exit rotation built on it.

Invariants:
  - The API key travels only in the X-API-Key header of a client that does
    not read the environment (no proxy settings) and never follows a redirect.
  - The control host is the proxy's own host, so there is no second address to
    get wrong. It is derived by control_host() and never logged.
  - A rotation always ends with a PUT "running", whatever happened in the
    middle. GluetunControl.we_stopped records an unanswered stop so shutdown
    can start the VPN again.
"""

# Standard library
import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit

# Third party
import httpx

CONTROL_PORT_DEFAULT = 8000
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_POLL_INTERVAL_S = 3.0


class GluetunError(Exception):
    """A control-server answer that was not 2xx. Carries the status only."""


class GluetunAuthError(GluetunError):
    """The control server answered 401: the key is wrong or its role lacks the route."""


def control_host(proxy_url: str) -> str:
    """
    Returns the host the control server shares with the proxy. Raises
    ValueError, without echoing the URL, when there is none or it cannot be
    parsed.
    """
    try:
        host = urlsplit(proxy_url).hostname
    except ValueError:
        host = None
    if not host:
        raise ValueError("AUDIBLE_PROXY_URL has no hostname")
    return host


class GluetunControl:
    def __init__(self, host: str, port: int, api_key: str) -> None:
        netloc = f"[{host}]" if ":" in host else host
        self.we_stopped = False
        self._http = httpx.AsyncClient(
            base_url=f"http://{netloc}:{port}",
            headers={"X-API-Key": api_key},
            timeout=_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        response = await self._http.request(method, path, **kwargs)
        if response.status_code == 401:
            raise GluetunAuthError("401")
        if not 200 <= response.status_code < 300:
            raise GluetunError(str(response.status_code))
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def status(self) -> str:
        return str((await self._request("GET", "/v1/vpn/status")).get("status", ""))

    async def public_ip(self) -> str:
        """The exit IP, or "" while the tunnel is down."""
        return str((await self._request("GET", "/v1/publicip/ip")).get("public_ip") or "")

    async def set_status(self, wanted: str) -> None:
        if wanted == "stopped":
            # Set before the PUT: an unanswered stop may still have landed.
            self.we_stopped = True
        await self._request("PUT", "/v1/vpn/status", json={"status": wanted})
        if wanted == "running":
            self.we_stopped = False

    async def ensure_running(self) -> None:
        await self.set_status("running")


@dataclass(frozen=True)
class RotationResult:
    old_ip: str
    new_ip: str
    ok: bool  # a new, non-empty IP; getting the old one back is a failed rotation


async def _wait_for(
    check: Callable[[], Awaitable[bool]], timeout: float, stop: asyncio.Event
) -> bool:
    """Polls check() until true, the timeout, or a stop request. Never raises on a false answer."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not stop.is_set():
        if await check():
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        try:
            await asyncio.wait_for(stop.wait(), _POLL_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
    return False


async def rotate(control: GluetunControl, stop: asyncio.Event, *, timeout: float = 60.0) -> RotationResult:
    """
    Stops and starts the VPN to draw a new exit. The start is in a finally, so
    an exception or a stop request after the stop cannot leave the tunnel
    down. Raises whatever the control server raised; a bad outcome (same IP,
    tunnel never came back) is a RotationResult with ok=False, not an exception.
    """
    old_ip = await control.public_ip()
    try:
        await control.set_status("stopped")
        await _wait_for(lambda: _is(control, "stopped"), 10.0, stop)
    finally:
        await control.ensure_running()

    if not await _wait_for(lambda: _is(control, "running"), timeout, stop):
        return RotationResult(old_ip, "", False)

    new_ip = ""

    async def _have_ip() -> bool:
        nonlocal new_ip
        new_ip = await control.public_ip()
        return bool(new_ip)

    await _wait_for(_have_ip, timeout, stop)
    return RotationResult(old_ip, new_ip, bool(new_ip) and new_ip != old_ip)


async def _is(control: GluetunControl, wanted: str) -> bool:
    return await control.status() == wanted
