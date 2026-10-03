"""
SOCKS5 proxy support in LibexClient: which schemes are accepted, that a port
is always required, that a missing socksio fails at construction with the
install hint, that no credential in a SOCKS URL reaches an exception, a
summary, a repr or a log record, and -- against a stub SOCKS5 server in a
child process -- that the CONNECT request names Audible by domain (ATYP 0x03)
for both socks5 and socks5h, so the proxy resolves the name and this machine
never does.
"""

# Standard library
import logging
import socket
import struct
import threading
from unittest.mock import patch

# Third party
import pytest

# Local
from libex_core.audible.client import LibexClient
from tests.libex_core._cli_support import clean_env, run_python

SENTINEL = "SENTINEL-s3cr3t-9f2c"
SCHEME_MESSAGE = "proxy URL must use the http, https, socks5 or socks5h scheme"
PORT_MESSAGE = "proxy URL must include a host and a valid port"
EXTRA_HINT = 'pip install "libex-core[socks]"'


# ============================================================
# SCHEMES AND PORTS
# ============================================================

@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_socks5_schemes_are_accepted_alike(scheme, caplog):
    with caplog.at_level(logging.DEBUG, logger="libex"):
        client = LibexClient(proxy_url=f"{scheme}://proxy.example.net:1080")
    summary = client.transport_summary()
    assert (summary.mode, summary.host, summary.scheme) == ("proxy", "proxy.example.net", scheme)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_http_schemes_still_accepted_and_report_their_scheme(scheme):
    summary = LibexClient(proxy_url=f"{scheme}://proxy.example.net:8080").transport_summary()
    assert summary.scheme == scheme


def test_direct_egress_has_no_scheme():
    assert LibexClient(proxy_url=None, allow_direct_egress=True).transport_summary().scheme is None


@pytest.mark.parametrize(
    "url", ["socks4://p.example.net:1080", "socks4a://p.example.net:1080", "ftp://p.example.net:21", "socks://p.example.net:1080"]
)
def test_other_schemes_are_rejected_with_static_text(url):
    with pytest.raises(ValueError) as exc_info:
        LibexClient(proxy_url=url)
    assert str(exc_info.value) == SCHEME_MESSAGE
    assert url not in str(exc_info.value)


@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_a_socks_url_without_a_port_is_rejected_and_no_port_is_guessed(scheme):
    with pytest.raises(ValueError) as exc_info:
        LibexClient(proxy_url=f"{scheme}://proxy.example.net")
    assert str(exc_info.value) == PORT_MESSAGE


# ============================================================
# MISSING socksio FAILS AT CONSTRUCTION
# ============================================================

@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_missing_socksio_raises_at_construction_with_the_hint(scheme):
    with patch("importlib.util.find_spec", return_value=None) as spec:
        with pytest.raises(ValueError) as exc_info:
            LibexClient(proxy_url=f"{scheme}://user:{SENTINEL}@proxy.example.net:1080")
    spec.assert_called_with("socksio")
    assert EXTRA_HINT in str(exc_info.value)
    assert SENTINEL not in str(exc_info.value)
    assert exc_info.value.__suppress_context__ is True


def test_http_proxies_do_not_need_socksio():
    with patch("importlib.util.find_spec", return_value=None):
        LibexClient(proxy_url="http://proxy.example.net:8080")


# ============================================================
# CREDENTIALS NEVER SURFACE
# ============================================================

@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_a_socks_credential_never_surfaces(scheme, caplog):
    url = f"{scheme}://user:{SENTINEL}@proxy.example.net:1080"
    with caplog.at_level(logging.DEBUG, logger="libex"):
        client = LibexClient(proxy_url=url)
        summary = client.transport_summary()
    for text in (repr(client), repr(summary), str(summary), caplog.text):
        assert SENTINEL not in text
    assert "user" not in repr(summary)


@pytest.mark.parametrize(
    "url",
    [
        f"socks4://user:{SENTINEL}@proxy.example.net:1080",
        f"socks5://user:{SENTINEL}@proxy.example.net",
        f"socks5://user:{SENTINEL}@:1080",
        f"socks5://user:{SENTINEL}@proxy.example.net:99999",
        f"user:{SENTINEL}@proxy.example.net:1080",
    ],
)
def test_a_rejected_socks_url_never_echoes_the_credential(url, caplog):
    with caplog.at_level(logging.DEBUG, logger="libex"):
        with pytest.raises(ValueError) as exc_info:
            LibexClient(proxy_url=url)
    assert SENTINEL not in str(exc_info.value)
    assert SENTINEL not in repr(exc_info.value)
    assert SENTINEL not in caplog.text
    assert exc_info.value.__cause__ is None


# ============================================================
# WIRE: THE CONNECT NAMES THE HOST, NEVER AN ADDRESS
# ============================================================

ATYP_IPV4, ATYP_DOMAIN, ATYP_IPV6 = 0x01, 0x03, 0x04

# Run in a child because it needs real sockets, which the unit suite blocks
# in this process. Every outcome of the request is swallowed: the stub
# refuses the CONNECT, and only what the stub received is under test.
_CHILD = """
import asyncio, sys
from libex_core.audible.client import LibexClient

async def main():
    async with LibexClient(proxy_url=sys.argv[1]) as client:
        try:
            await client.get("us", "/1.0/catalog/products/B000000000")
        except Exception:
            pass

asyncio.run(main())
"""


class _StubSocks5:
    """Negotiates no-auth, records the CONNECT request, then refuses it."""

    def __init__(self):
        self.request = b""
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(5)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self._server.settimeout(15)
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(10)
            conn.recv(256)
            conn.sendall(b"\x05\x00")
            self.request = conn.recv(4096)
            conn.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)

    def close(self):
        self._server.close()
        self._thread.join(timeout=5)


@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_connect_carries_the_domain_name_not_an_address(tmp_path, scheme):
    stub = _StubSocks5()
    try:
        run_python(
            ["-c", _CHILD, f"{scheme}://127.0.0.1:{stub.port}"],
            env=clean_env(),
            cwd=tmp_path,
            timeout=25,
        )
    finally:
        stub.close()
    request = stub.request
    assert request, "the stub never received a CONNECT"
    version, command, _reserved, atyp = request[:4]
    assert (version, command) == (0x05, 0x01)
    assert atyp == ATYP_DOMAIN, f"address type {atyp:#04x}, expected a domain name"
    length = request[4]
    name = request[5 : 5 + length]
    port = struct.unpack("!H", request[5 + length : 7 + length])[0]
    assert name == b"api.audible.com"
    assert port == 443
