"""
A PoolTimeout on the Audible client means httpcore has leaked connection
slots it never hands back, so the client is replaced; every other failure
leaves it alone. Requests are answered by patching httpx.AsyncClient.get, as
test_transport.py does.
"""

# Standard library
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import httpx
import pytest

# Local
import libex_core.audible._concurrency as concurrency_module
import libex_core.audible.client as client_module
from libex_core.audible.client import LibexClient
from libex_core.exceptions import AudibleAPIException, NotFoundException
from scripts import refresh_corpus

PATH = "/1.0/catalog/products"


def _client():
    return LibexClient(proxy_url=None, allow_direct_egress=True)


def _ok():
    response = MagicMock()
    response.status_code = 200
    response.headers = {}
    response.json.return_value = {"ok": True}
    return response


async def _build(client):
    """One good request, so the instance holds a live httpx client."""
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_ok())):
        await client.get("us", PATH, {"page": 0})
    return client._LibexClient__http


async def _let_closes_finish():
    pending = list(client_module._pending_client_closes)
    if pending:
        await asyncio.gather(*pending)


@pytest.mark.asyncio
async def test_pool_timeout_replaces_the_client_and_closes_the_old_one(caplog):
    client = _client()
    old = await _build(client)

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.PoolTimeout("pool"))):
        with caplog.at_level(logging.WARNING, logger="libex"):
            with pytest.raises(AudibleAPIException) as exc:
                await client.get("us", PATH, {"page": 0})
    # still the transient kind: an outage, never an absence
    assert not isinstance(exc.value, NotFoundException)
    assert exc.value.upstream_status is None
    record = next(r for r in caplog.records if r.getMessage() == "Audible HTTP pool exhausted, replacing the client")
    assert record.region == "us" and record.path == PATH

    new = await _build(client)
    assert new is not old
    await _let_closes_finish()
    assert old.is_closed
    assert not new.is_closed
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.WriteTimeout("slow"),
        httpx.ConnectError("refused"),
        httpx.RemoteProtocolError("cut"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_any_other_failure_keeps_the_client(error, caplog):
    """A slow or failing Audible is not a leaked pool: replacing the client
    for it would throw away every warm connection for nothing."""
    client = _client()
    live = await _build(client)
    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=error)):
        with caplog.at_level(logging.WARNING, logger="libex"):
            with pytest.raises(AudibleAPIException):
                await client.get("us", PATH, {"page": 0})
    assert client._LibexClient__http is live
    assert not any("pool exhausted" in r.getMessage() for r in caplog.records)
    await client.aclose()


@pytest.mark.asyncio
async def test_a_late_pool_timeout_from_a_replaced_client_keeps_the_replacement():
    """Two requests queued on the same wedged pool time out at different
    moments. The second one's PoolTimeout arrives after the first already
    caused a replacement; it must not throw that healthy replacement away."""
    client = _client()
    old = await _build(client)
    gate = asyncio.Event()
    stuck = {"n": 0}

    async def fake_get(self, url, **kwargs):
        if self is old:
            if stuck["n"] == 0:
                stuck["n"] += 1
                await gate.wait()  # request A: still queued in the old pool
            raise httpx.PoolTimeout("pool")
        return _ok()

    with patch.object(httpx.AsyncClient, "get", autospec=True, side_effect=fake_get):
        a = asyncio.ensure_future(client.get("us", PATH, {"page": 0}))
        await asyncio.sleep(0)
        with pytest.raises(AudibleAPIException):  # request B times out first
            await client.get("us", PATH, {"page": 0})
        await client.get("us", PATH, {"page": 0})  # builds the replacement
        new = client._LibexClient__http
        assert new is not old
        gate.set()
        with pytest.raises(AudibleAPIException):  # request A's late PoolTimeout
            await a
    assert client._LibexClient__http is new
    await client.aclose()


@pytest.mark.asyncio
async def test_a_burst_of_pool_timeouts_builds_exactly_one_replacement():
    client = _client()
    old = await _build(client)
    gate = asyncio.Event()
    seen = set()

    async def fake_get(self, url, **kwargs):
        seen.add(self)
        if self is old:
            await gate.wait()
            raise httpx.PoolTimeout("pool")
        return _ok()

    with patch.object(httpx.AsyncClient, "get", autospec=True, side_effect=fake_get):
        burst = [asyncio.ensure_future(client.get("us", PATH, {"page": i})) for i in range(5)]
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(*burst, return_exceptions=True)
        assert all(isinstance(r, AudibleAPIException) for r in results)
        await asyncio.gather(*(client.get("us", PATH, {"page": i}) for i in range(3)))
    assert len(seen) == 2  # the wedged client and one replacement
    assert client._LibexClient__http in seen and client._LibexClient__http is not old
    await client.aclose()


@pytest.mark.asyncio
async def test_a_late_retire_never_reopens_a_closed_instance():
    client = _client()
    old = await _build(client)
    await client.aclose()
    client._retire_client(old)
    assert client.is_open is False
    with pytest.raises(RuntimeError):
        await client.get("us", PATH, {"page": 0})


# The retire rests on this: every request holds a semaphore permit, so a pool
# at least as large as the permits can only run dry by leaking.

def test_pool_is_at_least_the_permit_sum():
    permits = (
        concurrency_module.AUDIBLE_CONCURRENCY_LIMIT
        + concurrency_module.AUDIBLE_AUTHOR_BOOKS_CONCURRENCY_LIMIT
    )
    assert client_module._AUDIBLE_POOL_LIMITS.max_connections >= permits


def test_pool_still_covers_the_permits_after_the_refresh_resize(monkeypatch):
    monkeypatch.setattr(concurrency_module, "AUDIBLE_CONCURRENCY_LIMIT", 10)
    monkeypatch.setattr(concurrency_module, "_audible_semaphore", None)
    monkeypatch.setattr(concurrency_module, "_audible_semaphore_loop", None)
    monkeypatch.setattr(client_module, "_AUDIBLE_POOL_LIMITS", client_module._AUDIBLE_POOL_LIMITS)
    monkeypatch.setattr(refresh_corpus.persist_queue, "_bg_write_semaphore", None)
    monkeypatch.setattr(
        refresh_corpus.persist_queue, "_BG_WRITE_CONCURRENCY_LIMIT",
        refresh_corpus.persist_queue._BG_WRITE_CONCURRENCY_LIMIT,
    )

    refresh_corpus._raise_process_limits()

    # the refresh run never enters the author-books gate, so the default
    # gate's permits are all it can hold
    assert (
        client_module._AUDIBLE_POOL_LIMITS.max_connections
        >= concurrency_module.AUDIBLE_CONCURRENCY_LIMIT
    )
    assert concurrency_module.AUDIBLE_CONCURRENCY_LIMIT == refresh_corpus.CONCURRENCY_MAX
