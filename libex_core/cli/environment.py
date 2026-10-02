"""
The only place libex_core reads the process environment.

Everything else in the package takes its settings as arguments, so an
embedder's own environment never changes what the library does. The command
line is the one caller that has no code to pass arguments from, and a proxy
URL can carry credentials that must not appear on a command line where other
processes can read it, so it comes from here. Reads happen when load_config()
is called, never at import.
"""

import os
from dataclasses import dataclass, field

PROXY_URL_VARIABLE = "LIBEX_CORE_PROXY_URL"
ALLOW_DIRECT_EGRESS_VARIABLE = "LIBEX_CORE_ALLOW_DIRECT_EGRESS"

# Printed in the man page's ENVIRONMENT section, in this order.
VARIABLES: tuple[tuple[str, str], ...] = (
    (
        PROXY_URL_VARIABLE,
        "URL of the http or https proxy that every request goes through. "
        "It may carry credentials, so it is read from the environment and "
        "never accepted as an option. Empty means unset.",
    ),
    (
        ALLOW_DIRECT_EGRESS_VARIABLE,
        "Set to 1, true, yes or on to let requests leave on this machine's "
        "own address when no proxy is set. 0, false, no, off or empty keeps "
        "that refused. A proxy, when set, is always used.",
    ),
)

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})


class ConfigError(Exception):
    """Raised with fixed text only: the message never contains a value read
    from the environment, since one of them is a credential."""


@dataclass(frozen=True)
class Config:
    # Excluded from repr and comparison so a stray log line or assertion
    # failure cannot print the credential it may carry.
    proxy_url: str | None = field(repr=False, compare=False)
    allow_direct_egress: bool


def load_config() -> Config:
    raw_allow = os.environ.get(ALLOW_DIRECT_EGRESS_VARIABLE, "")
    normalized = raw_allow.strip().lower()
    if normalized in _TRUE:
        allow = True
    elif normalized in _FALSE:
        allow = False
    else:
        # Checked even when a proxy is set, so a typo is found on the first
        # run rather than the day the proxy is removed.
        raise ConfigError(
            f"{ALLOW_DIRECT_EGRESS_VARIABLE} must be one of 1, true, yes, on, "
            "0, false, no, off, or empty"
        ) from None
    proxy_url = os.environ.get(PROXY_URL_VARIABLE) or None
    return Config(proxy_url=proxy_url, allow_direct_egress=allow)
