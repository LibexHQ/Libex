"""
A connect failure never reached Audible, so it is resent once; anything that
might have been seen, or that is a timeout, is not, and a client closed or
retired under the request is never retried through. Requests are answered by
patching httpx.AsyncClient.get, as test_pool_retire.py does.
"""

# Standard library
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import httpx
import pytest

# Local
from libex_core.audible.client import LibexClient
from libex_core.exceptions import AudibleAPIException

PATH = "/1.0/catalog/products"


def _client():
    return LibexClient(proxy_url=None, allow_direct_egress=True)


def _ok():
    response = MagicMock()
    response.status_code = 200
    response.headers = {}
    response.json.return_value = {"ok": True}
    return response


BACKOFF = 0.123


def _status(code):
    response = MagicMock()
    response.status_code = code
    response.headers = {}
    return response


async def _run(client, side_effect):
    mock = AsyncMock(side_effect=side_effect)
    sleep = AsyncMock()
    backoff = MagicMock(return_value=BACKOFF)
    with patch("httpx.AsyncClient.get", new=mock), patch(
        "libex_core.audible.client.asyncio.sleep", new=sleep
    ), patch("libex_core.audible.client._compute_backoff_seconds", new=backoff):
        try:
            return await client.get("us", PATH, {"page": 0}), mock, sleep, backoff
        except AudibleAPIException as exc:
            return exc, mock, sleep, backoff


async def _get(client, side_effect):
    result, mock, _, _ = await _run(client, side_effect)
    return result, mock


def _retry_records(caplog):
    return [
        r
        for r in caplog.records
        if r.getMessage() == "Audible connection failed, retrying once"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ProxyError])
async def test_connect_failure_then_success_returns_data(error_type):
    client = _client()
    result, mock = await _get(client, [error_type("drop"), _ok()])
    assert result == {"ok": True}
    assert mock.call_count == 2
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ProxyError])
async def test_connect_failure_twice_raises_after_two_calls(error_type):
    client = _client()
    result, mock = await _get(client, [error_type("drop"), error_type("drop"), _ok()])
    assert isinstance(result, AudibleAPIException)
    assert str(result).startswith(
        "Audible API request failed: drop for https://api.audible.com"
    )
    assert mock.call_count == 2
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadError("cut"),
        httpx.WriteError("cut"),
        httpx.RemoteProtocolError("cut"),
        httpx.ConnectTimeout("slow"),
        httpx.ReadTimeout("slow"),
        httpx.PoolTimeout("pool"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_other_failures_are_not_retried(error):
    client = _client()
    result, mock = await _get(client, [error, _ok()])
    assert isinstance(result, AudibleAPIException)
    assert mock.call_count == 1
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["retired", "closed"])
async def test_no_retry_when_client_retired_or_closed_under_the_request(how):
    client = _client()

    async def fail_after_teardown(*args, **kwargs):
        if how == "retired":
            client._retire_client(client._LibexClient__http)
        else:
            await client.aclose()
        raise httpx.ConnectError("drop")

    result, mock = await _get(client, fail_after_teardown)
    assert isinstance(result, AudibleAPIException)
    assert mock.call_count == 1
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ProxyError])
async def test_connect_retry_backs_off_once_and_logs_warning(error_type, caplog):
    client = _client()
    with caplog.at_level("WARNING", logger="libex_core.audible.client"):
        result, mock, sleep, backoff = await _run(client, [error_type("drop"), _ok()])
    assert result == {"ok": True}
    backoff.assert_called_once_with(0, None)
    sleep.assert_awaited_once_with(BACKOFF)
    records = _retry_records(caplog)
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert records[0].region == "us"
    assert records[0].path == PATH
    assert records[0].error_type == error_type.__name__
    await client.aclose()


@pytest.mark.asyncio
async def test_retry_resends_the_same_url_and_params():
    client = _client()
    _, mock = await _get(client, [httpx.ConnectError("drop"), _ok()])
    assert mock.call_count == 2
    first, second = mock.call_args_list
    assert first.args[0].startswith("https://api.audible.com")
    assert second.args == first.args
    assert second.kwargs == first.kwargs
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [httpx.ReadError("cut"), httpx.ConnectTimeout("slow")],
    ids=lambda e: type(e).__name__,
)
async def test_non_retried_error_neither_sleeps_nor_logs_retry(error, caplog):
    client = _client()
    with caplog.at_level("WARNING", logger="libex_core.audible.client"):
        result, mock, sleep, backoff = await _run(client, [error, _ok()])
    assert isinstance(result, AudibleAPIException)
    assert mock.call_count == 1
    sleep.assert_not_awaited()
    backoff.assert_not_called()
    assert _retry_records(caplog) == []
    await client.aclose()


@pytest.mark.asyncio
async def test_connect_failure_on_last_attempt_is_not_retried(caplog):
    client = _client()
    with caplog.at_level("WARNING", logger="libex_core.audible.client"):
        result, mock, sleep, _ = await _run(
            client, [_status(503), _status(503), httpx.ConnectError("drop"), _ok()]
        )
    assert isinstance(result, AudibleAPIException)
    assert str(result).startswith(
        "Audible API request failed: drop for https://api.audible.com"
    )
    assert mock.call_count == 3
    # Only the two status-code backoffs; a connect retry would add a third.
    assert sleep.await_count == 2
    assert _retry_records(caplog) == []
    await client.aclose()


@pytest.mark.asyncio
async def test_connect_retry_and_status_retries_share_one_budget():
    client = _client()
    result, mock, sleep, _ = await _run(
        client, [httpx.ConnectError("drop"), _status(429), _status(429), _ok()]
    )
    assert isinstance(result, AudibleAPIException)
    assert result.upstream_status == 429
    assert mock.call_count == 3
    assert sleep.await_count == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_connect_retry_then_status_retry_then_success_fits_the_budget():
    client = _client()
    result, mock, _, _ = await _run(
        client, [httpx.ConnectError("drop"), _status(429), _ok()]
    )
    assert result == {"ok": True}
    assert mock.call_count == 3
    await client.aclose()


@pytest.mark.asyncio
async def test_no_retry_when_only_the_closed_flag_is_set(caplog):
    client = _client()

    async def fail_with_flag_only(*args, **kwargs):
        # The flag alone, with the live httpx client untouched, so the
        # identity check cannot be what stops the retry.
        client._closed = True
        raise httpx.ConnectError("drop")

    with caplog.at_level("WARNING", logger="libex_core.audible.client"):
        result, mock, sleep, _ = await _run(client, fail_with_flag_only)
    assert isinstance(result, AudibleAPIException)
    assert mock.call_count == 1
    sleep.assert_not_awaited()
    assert _retry_records(caplog) == []
    client._closed = False
    await client.aclose()
