"""
LibexClient.get on a 200 whose body is not JSON: an HTML interstitial, an
empty body or a truncated one. Audible answered, but with nothing readable,
so it is an outage (AudibleAPIException, upstream_status 200), never a bare
ValueError and never a NotFoundException. A valid JSON 200 is unchanged.

Answered by a real httpx.AsyncClient over an in-process MockTransport so the
real Response.json() is what raises.
"""

# Standard library
import logging
from unittest.mock import patch

# Third party
import httpx
import pytest

# Local
from libex_core.audible.client import LibexClient
from libex_core.exceptions import AudibleAPIException, NotFoundException

SECRET_MARKER = "interstitial-body-marker-7731"

NON_JSON_BODIES = [
    pytest.param(f"<html><body>{SECRET_MARKER}</body></html>", id="html"),
    pytest.param("", id="empty"),
    pytest.param('{"products": [{"asin": "B0TRUNC001", "title": "' + SECRET_MARKER, id="truncated"),
]


def _client_answering(body):
    client = LibexClient(proxy_url=None, allow_direct_egress=True)
    calls = {"n": 0}

    def _handler(request):
        calls["n"] += 1
        return httpx.Response(200, content=body.encode())

    real_cls = httpx.AsyncClient

    def _fake(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_cls(*args, **kwargs)

    return client, calls, patch("libex_core.audible.client.httpx.AsyncClient", side_effect=_fake)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", NON_JSON_BODIES)
async def test_non_json_200_raises_audible_api_exception_with_upstream_status_200(body):
    client, calls, fake = _client_answering(body)
    with fake:
        with pytest.raises(AudibleAPIException) as exc_info:
            await client.get("us", "/1.0/catalog/products")
    assert not isinstance(exc_info.value, NotFoundException)
    assert exc_info.value.upstream_status == 200
    assert exc_info.value.status_code == 502
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert calls["n"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", NON_JSON_BODIES)
async def test_non_json_200_message_and_log_never_carry_the_body(body, caplog):
    client, _, fake = _client_answering(body)
    with fake, caplog.at_level(logging.DEBUG, logger="libex"):
        with pytest.raises(AudibleAPIException) as exc_info:
            await client.get("us", "/1.0/catalog/products")
    assert SECRET_MARKER not in exc_info.value.message
    assert SECRET_MARKER not in caplog.text
    records = [r for r in caplog.records if "not JSON" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].region == "us"
    # extra= fields are attributes on the record, not part of caplog.text.
    assert SECRET_MARKER not in repr(vars(records[0]))


@pytest.mark.asyncio
async def test_valid_json_200_is_returned_unchanged():
    client, _, fake = _client_answering('{"products": [{"asin": "B0VALID001"}]}')
    with fake:
        result = await client.get("us", "/1.0/catalog/products")
    assert result == {"products": [{"asin": "B0VALID001"}]}
