"""
Gotify notifications.

Invariants:
  - Sent direct, never through the Audible proxy, by a client that reads no
    environment and follows no redirect.
  - The token travels only in the X-Gotify-Key header.
  - GOTIFY_URL carries no userinfo and is https unless its host is private or
    on the LAN, so the token is not sent in the clear across the internet.
  - A failed send is logged by error type and returned as False; it never
    raises, so a down notifier cannot stop the watch loop.
"""

# Standard library
import ipaddress
from urllib.parse import urlsplit

# Third party
import httpx

# Internal
from app.core.logging import get_logger

logger = get_logger()

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_LAN_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".localdomain")


def _is_private_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A name is judged by its shape (a single label such as a compose
        # service name, or a LAN suffix) and is never resolved, so a public
        # name that points at a private address still has to use https.
        return host == "localhost" or "." not in host or host.endswith(_LAN_SUFFIXES)
    return ip.is_private or ip.is_loopback or ip.is_link_local


def validate_url(url: str) -> str:
    """Returns the URL without a trailing slash, or raises ValueError that never echoes it."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port
    except ValueError:
        raise ValueError("GOTIFY_URL could not be parsed") from None
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("GOTIFY_URL must be an http(s) URL with a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("GOTIFY_URL must not contain credentials")
    if parts.scheme == "http" and not _is_private_host(host):
        raise ValueError("GOTIFY_URL must be https unless the host is private or on the LAN")
    return url.rstrip("/")


class Notifier:
    def __init__(self, url: str, token: str, stack: str) -> None:
        self._stack = stack
        self._http = httpx.AsyncClient(
            base_url=validate_url(url),
            headers={"X-Gotify-Key": token},
            timeout=_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send(self, what: str, happened: str, did: str, needs_you: bool) -> bool:
        """
        One message per transition. The title is "[stack] what"; the body is
        three lines: what happened, what was done, whether the operator is
        needed. Callers pass no log content and no caller addresses.
        """
        body = f"What happened: {happened}\nWhat I did: {did}\nNeeds you: {'yes' if needs_you else 'no'}"
        try:
            response = await self._http.post(
                "/message",
                json={"title": f"[{self._stack}] {what}", "message": body, "priority": 8 if needs_you else 5},
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning(
                "Watch: notification not delivered",
                extra={"error_type": type(exc).__name__, "alert": what},
            )
            return False
        return True
