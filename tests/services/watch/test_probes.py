"""
Tests for the exit probe classifier and probe_exit's block verdict.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import httpx
import pytest

# Core
from libex_core.exceptions import AudibleAPIException, NotFoundException

# Services
from app.services.watch import probes
from app.services.watch.probes import ExitProbe, classify, probe_exit


def _raised(cause=None, upstream_status=None):
    """An AudibleAPIException with an implicit chain, as the client raises it."""
    try:
        try:
            raise cause if cause is not None else ValueError("plain")
        except Exception:
            raise AudibleAPIException("x", upstream_status=upstream_status)
    except AudibleAPIException as exc:
        return exc


# --- classify ----------------------------------------------------------------

@pytest.mark.parametrize("exc", [
    _raised(upstream_status=403),
    _raised(upstream_status=429),
    _raised(cause=httpx.ReadTimeout("t")),
    _raised(cause=httpx.PoolTimeout("t")),
    _raised(cause=httpx.ProxyError("p")),
    _raised(cause=httpx.ConnectError("c")),
], ids=["403", "429", "read-timeout", "pool-timeout", "proxy-error", "connect-error"])
def test_block_classes_are_block(exc):
    assert classify(exc) == "block"


@pytest.mark.parametrize("exc", [
    _raised(upstream_status=500),
    _raised(upstream_status=503),
    _raised(upstream_status=404),
    _raised(cause=ValueError("not json")),   # 200 with a non-JSON body
    _raised(cause=httpx.ReadError("r")),     # bare ReadError says nothing about the exit
    NotFoundException("gone"),
    RuntimeError("boom"),
], ids=["500", "503", "404", "non-json-200", "bare-read-error", "not-found", "non-audible"])
def test_non_block_classes_are_other(exc):
    assert classify(exc) == "other"


def test_upstream_status_wins_over_a_block_looking_cause():
    # A real 500 with a timeout in the chain is still a 5xx: not a block.
    assert classify(_raised(cause=httpx.ReadTimeout("t"), upstream_status=500)) == "other"


# --- ExitProbe.blocked -------------------------------------------------------

@pytest.mark.parametrize("verdicts,blocked", [
    ({"us": "block", "uk": "block"}, True),
    ({"us": "block", "uk": "ok"}, False),
    ({"us": "block", "uk": "other"}, False),
    ({"us": "ok", "uk": "ok"}, False),
])
def test_blocked_needs_every_region_blocked(verdicts, blocked):
    assert ExitProbe(verdicts).blocked is blocked


# --- probe_exit --------------------------------------------------------------

def _patches(statuses):
    """statuses: region -> upstream status to raise, or None for success."""
    async def fake_fetch(get, asins, region):
        if statuses[region] is not None:
            raise _raised(upstream_status=statuses[region])

    return patch.object(probes, "fetch_products", fake_fetch), patch.object(probes, "LibexClient")


async def test_probe_exit_one_region_blocked_is_not_a_blocked_cycle():
    p_fetch, p_client = _patches({"us": 403, "uk": None})
    with p_fetch, p_client as client_cls:
        client_cls.return_value.aclose = AsyncMock()
        result = await probe_exit("http://gluetun:8888")
    assert result.verdicts == {"us": "block", "uk": "ok"}
    assert result.blocked is False
    client_cls.assert_called_once_with(proxy_url="http://gluetun:8888")
    client_cls.return_value.aclose.assert_awaited_once()


async def test_probe_exit_5xx_in_both_regions_is_not_blocked():
    p_fetch, p_client = _patches({"us": 503, "uk": 500})
    with p_fetch, p_client as client_cls:
        client_cls.return_value.aclose = AsyncMock()
        result = await probe_exit("http://gluetun:8888")
    assert result.verdicts == {"us": "other", "uk": "other"}
    assert result.blocked is False
    client_cls.return_value.aclose.assert_awaited_once()


async def test_probe_exit_both_blocked():
    p_fetch, p_client = _patches({"us": 429, "uk": 403})
    with p_fetch, p_client as client_cls:
        client_cls.return_value.aclose = AsyncMock()
        assert (await probe_exit("http://gluetun:8888")).blocked is True
