"""
libex_core.audible.client.LibexClient's whole contract: how egress is
decided once at construction and never replaced, what it validates, what it
exposes, how a request is retried, how it shares process-wide concurrency
with every other LibexClient in the same process, and how aclose() makes an
instance permanently refuse further use.

This module carries no dependency on the hosted application and no
dependency on Postgres or a real network -- see test_isolation.py for the
subprocess/AST proof of the first, and tests/conftest.py's autouse
block_network_sockets fixture for the second, which applies here exactly as
it does everywhere else in the suite. Every test below that builds a real
httpx.AsyncClient answers it with an in-process httpx.MockTransport rather
than a mocked .get(), specifically where the real closed-client check inside
AsyncClient.send() is the thing being proved; everywhere else, patching
httpx.AsyncClient.get directly is simpler and just as honest, since nothing
in those tests depends on that internal check.
"""

# Standard library
import asyncio
import dataclasses
import inspect
import subprocess
import sys
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import httpx
import pytest

# Local
import libex_core.audible.client as client_module
from libex_core.audible.client import LibexClient, get_audible_url
from libex_core.exceptions import AudibleAPIException, NotFoundException

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _client(proxy_url=None, allow_direct_egress=True):
    return LibexClient(proxy_url=proxy_url, allow_direct_egress=allow_direct_egress)


def _mock_response(status_code, headers=None, json_body=None):
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.json.return_value = json_body if json_body is not None else {"ok": True}
    return response


# ============================================================
# CONSTRUCTOR -- ARITY, BLANK-PROXY REFUSAL, MALFORMED VALUES
# ============================================================

def test_libexclient_requires_keyword_arguments():
    """There is no default for proxy_url -- omitting it is a TypeError from
    Python's own argument checking, not a runtime guard this class has to
    remember to enforce, which is what makes "nobody ever decided" an
    unrepresentable state rather than one this module has to detect."""
    with pytest.raises(TypeError):
        LibexClient()


def test_none_without_allow_direct_egress_raises_value_error():
    with pytest.raises(ValueError) as exc_info:
        LibexClient(proxy_url=None)
    assert "allow_direct_egress" in str(exc_info.value)


def test_none_without_allow_direct_egress_message_is_pinned():
    """The message is fixed text, never built from proxy_url -- there's
    nothing to leak from a blank value, but pinning this keeps the message
    from silently drifting later without a test noticing."""
    with pytest.raises(ValueError) as exc_info:
        LibexClient(proxy_url=None)
    assert str(exc_info.value) == (
        "a blank or missing proxy URL does not configure direct "
        "egress by itself -- pass allow_direct_egress=True to "
        "LibexClient() if egressing to Audible without a proxy "
        "is really what's intended, so a proxy setting that "
        "came through empty by mistake fails loudly instead of "
        "sending every request out on this process's own IP"
    )


def test_empty_string_without_allow_direct_egress_raises_value_error():
    with pytest.raises(ValueError) as exc_info:
        LibexClient(proxy_url="")
    assert "allow_direct_egress" in str(exc_info.value)


def test_none_with_allow_direct_egress_configures_direct_egress():
    client = LibexClient(proxy_url=None, allow_direct_egress=True)
    summary = client.transport_summary()
    assert summary.mode == "direct"
    assert summary.host is None


def test_empty_string_with_allow_direct_egress_configures_direct_egress():
    client = LibexClient(proxy_url="", allow_direct_egress=True)
    summary = client.transport_summary()
    assert summary.mode == "direct"
    assert summary.host is None


def test_a_real_proxy_url_succeeds_regardless_of_allow_direct_egress():
    """Proves the flag is only ever consulted in the blank branch -- a
    supplied proxy URL configures proxy mode whether allow_direct_egress is
    left at its False default or passed explicitly."""
    client = LibexClient(proxy_url="http://libex-one-vpn:8888", allow_direct_egress=False)
    summary = client.transport_summary()
    assert summary.mode == "proxy"
    assert summary.host == "libex-one-vpn"


@pytest.mark.parametrize(
    "case, value, expected_message",
    [
        ("missing scheme", "libex-seeder-vpn:8888", "proxy URL could not be parsed"),
        ("bad scheme", "socks5://libex-seeder-vpn:8888", "proxy URL must use the http or https scheme"),
        ("empty host", "http://:8888", "proxy URL must include a host and a valid port"),
        ("bad port", "http://libex-seeder-vpn:999999", "proxy URL must include a host and a valid port"),
    ],
)
def test_constructor_raises_on_malformed_proxy_value(case, value, expected_message):
    with pytest.raises(ValueError) as exc_info:
        LibexClient(proxy_url=value)
    assert str(exc_info.value) == expected_message


def test_constructor_error_never_names_the_credentialed_value():
    """A scheme-less or otherwise malformed value can carry embedded
    credentials (user:pass@host:port) -- neither the raised message nor the
    traceback a caller would actually see (via `from None`, which suppresses
    the chained original httpx exception from ever being formatted in) may
    contain any part of it."""
    credentialed = "http://opsuser:s3cr3t-token@libex-seeder-vpn:notaport"

    with pytest.raises(ValueError) as exc_info:
        LibexClient(proxy_url=credentialed)

    exc = exc_info.value
    formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert "s3cr3t-token" not in str(exc)
    assert "opsuser" not in str(exc)
    assert "s3cr3t-token" not in formatted
    assert "opsuser" not in formatted
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True


# ============================================================
# TRANSPORT_SUMMARY() -- NEVER THE URL, NEVER CREDENTIALS
# ============================================================

def test_transport_summary_carries_only_mode_and_host():
    client = LibexClient(proxy_url="http://opsuser:s3cr3t-token@libex-seeder-vpn:8888")
    summary = client.transport_summary()

    field_names = {f.name for f in dataclasses.fields(summary)}
    assert field_names == {"mode", "host"}


def test_transport_summary_never_exposes_the_url_or_credentials():
    client = LibexClient(proxy_url="http://opsuser:s3cr3t-token@libex-seeder-vpn:8888")
    summary = client.transport_summary()

    assert summary.host == "libex-seeder-vpn"
    text = repr(summary) + str(summary)
    assert "opsuser" not in text
    assert "s3cr3t-token" not in text
    assert "http://" not in text


@pytest.mark.asyncio
async def test_transport_summary_unaffected_by_aclose_or_a_client_rebuild():
    client = LibexClient(proxy_url="http://libex-one-vpn:8888")
    before = client.transport_summary()

    client._get_client()
    await client.aclose()

    assert client.transport_summary() == before


# ============================================================
# _PROXY -- READ-ONLY, AND THE EXACT OBJECT THE TRAFFIC USES
# ============================================================

def test_proxy_property_has_no_setter():
    client = LibexClient(proxy_url="http://libex-one-vpn:8888")
    with pytest.raises(AttributeError):
        client._proxy = None


def test_proxy_property_is_none_in_direct_mode():
    client = LibexClient(proxy_url=None, allow_direct_egress=True)
    assert client._proxy is None


@pytest.mark.asyncio
async def test_proxy_property_is_the_exact_object_the_http_client_is_built_with():
    """
    The fix behind moving this off a module-level singleton, in one
    assertion: _proxy must be the SAME object -- identity, not merely one
    that looks equal because it was rebuilt from the same URL -- that this
    instance's real httpx.AsyncClient is actually built with. A reader of
    _proxy (an operator script's _verify_dedicated_proxy, or
    backfill_chapters._log_exit_ip's own probe client) is trusting that what
    it inspects IS what carries this instance's traffic; a re-derived proxy
    object that only happens to be equal could silently drift from what a
    live request actually uses. Per-instance state makes "the guard passed"
    and "the traffic used that exit" the same fact rather than two joined
    only by convention.
    """
    client = LibexClient(proxy_url="http://libex-one-vpn:8888")
    proxy_obj = client._proxy

    captured = {}
    real_async_client_cls = httpx.AsyncClient

    def _capture(*args, **kwargs):
        captured["proxy"] = kwargs.get("proxy")
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        return real_async_client_cls(*args, **kwargs)

    with patch("libex_core.audible.client.httpx.AsyncClient", side_effect=_capture):
        client._get_client()

    assert captured["proxy"] is proxy_obj
    await client.aclose()


# ============================================================
# IS_OPEN -- TRUE ONLY WITH A LIVE CLIENT RIGHT NOW
# ============================================================

def test_is_open_false_when_never_built():
    client = _client()
    assert client.is_open is False


@pytest.mark.asyncio
async def test_is_open_true_once_a_live_client_exists():
    client = _client()
    client._get_client()
    assert client.is_open is True
    await client.aclose()


@pytest.mark.asyncio
async def test_is_open_false_after_aclose():
    """is_open is deliberately NOT the complement of httpx's own
    AsyncClient.is_closed -- both "never built a client yet" and "aclose()
    has already run" must read False, which a naive `not
    self.__http.is_closed` (raising on a None client) or one that conflates
    the two states would get wrong."""
    client = _client()
    client._get_client()
    await client.aclose()
    assert client.is_open is False


def test_is_open_false_never_built_is_distinguished_from_closed_only_by_flag():
    """A fresh, never-touched instance and a closed one both read is_open ==
    False -- proving they are still two different internal states (rather
    than the same one) is what the aclose()-refuses-get() tests below cover;
    this test only pins the shared observable."""
    fresh = _client()
    assert fresh.is_open is False
    assert fresh._closed is False


# ============================================================
# ACLOSE() -- TERMINAL, FAIL-CLOSED, IDEMPOTENT
# ============================================================

@pytest.mark.asyncio
async def test_aclose_before_any_get_is_a_noop_and_does_not_raise():
    client = _client()
    await client.aclose()  # must not raise
    assert client.is_open is False
    assert client._closed is True


@pytest.mark.asyncio
async def test_aclose_is_idempotent():
    client = _client()
    await client.aclose()
    await client.aclose()  # second call must not raise


@pytest.mark.asyncio
async def test_get_raises_runtime_error_after_aclose():
    client = _client()
    await client.aclose()

    with pytest.raises(RuntimeError, match="has been closed"):
        await client.get("us", "/1.0/catalog/products")


@pytest.mark.asyncio
async def test_aclose_refuses_before_acquiring_a_semaphore_permit():
    """A closed instance's get() must refuse before ever touching the
    shared, process-wide semaphore -- consuming a permit on a call that is
    refused anyway would starve a live caller of a slot for nothing."""
    client = _client()
    await client.aclose()

    with patch("libex_core.audible.client._current_audible_semaphore") as mock_sem_getter:
        with pytest.raises(RuntimeError, match="has been closed"):
            await client.get("us", "/1.0/catalog/products")

    mock_sem_getter.assert_not_called()


@pytest.mark.asyncio
async def test_get_client_rebuilds_and_background_closes_the_stale_one_on_a_loop_change():
    """
    The counterpart to the never-rebuilds-once-closed test below: a still-
    OPEN instance whose recorded loop no longer matches the current one must
    rebuild rather than reuse a client tied to a loop that has gone away,
    and the stale client must still get closed -- in the background, via
    _track_pending_close/asyncio.create_task, not left leaking connections.
    """
    client = _client()
    first = client._get_client()

    # Simulate a loop change the same way the module's own docstring
    # describes: force the recorded loop to something other than the one
    # actually running.
    client._LibexClient__http_loop = object()

    created_tasks = []
    real_create_task = asyncio.create_task

    def _tracking_create_task(coro, **kwargs):
        task = real_create_task(coro, **kwargs)
        created_tasks.append(task)
        return task

    with patch("libex_core.audible.client.asyncio.create_task", side_effect=_tracking_create_task):
        second = client._get_client()

    assert second is not first
    await asyncio.gather(*created_tasks)  # let the background close finish
    assert first.is_closed
    await client.aclose()


@pytest.mark.asyncio
async def test_get_client_never_rebuilds_after_close_even_across_a_loop_change():
    """
    The lazy builder must never rebuild once closed -- including across a
    change of running event loop, which is exactly the branch a naive "just
    discard the client reference" implementation gets wrong: a closed
    instance whose recorded loop no longer matches the current one would
    otherwise look, to _get_client, indistinguishable from one that has
    simply never been built on this loop yet, and would rebuild and egress
    right after a caller closed specifically to stop it.
    """
    client = _client()
    client._get_client()
    await client.aclose()

    # Force the recorded loop to something else entirely, simulating a loop
    # change -- the naive bug this guards against is "loop changed ->
    # rebuild", which must never be reached once _closed is True.
    client._LibexClient__http_loop = object()

    build_calls = []
    real_async_client_cls = httpx.AsyncClient

    def _tracking(*args, **kwargs):
        build_calls.append(1)
        return real_async_client_cls(*args, **kwargs)

    with patch("libex_core.audible.client.httpx.AsyncClient", side_effect=_tracking):
        with pytest.raises(RuntimeError, match="has been closed"):
            client._get_client()

    assert build_calls == []


@pytest.mark.asyncio
async def test_get_refuses_on_the_next_attempt_after_aclose_mid_retry():
    """
    This retargets a hazard test that used to name a mid-retry
    configure_transport() reconfigure, a feature that no longer exists. The
    underlying hazard is relocated, not closed: the danger was always the
    httpx client being closed under an in-flight request, and aclose()
    reproduces exactly that, at higher probability, since a caller closing
    this instance mid-retry (shutdown, revoked consent, a task simply done
    with this instance) is a realistic sequence a reconfigure API no longer
    is.

    _get_client's own closed check runs once per attempt (see get()'s own
    docstring), so a caller task closing this instance during the retry's
    own asyncio.sleep must be refused with the documented RuntimeError on
    the very next attempt, before a second real request goes out -- never as
    the bare, undocumented RuntimeError httpx itself raises internally on a
    closed client, which would escape `except httpx.RequestError` entirely
    rather than being reported as the closed-instance error it actually is.

    Proved with a real httpx.AsyncClient over an in-process MockTransport,
    not a mocked .get(): the closed-client check this is protecting against
    lives inside AsyncClient.send() itself.
    """
    client = _client(proxy_url="http://libex-one-vpn:8888", allow_direct_egress=False)

    call_count = {"n": 0}

    def _handler(request):
        call_count["n"] += 1
        return httpx.Response(429)

    real_async_client_cls = httpx.AsyncClient

    def _fake_async_client(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async_client_cls(*args, **kwargs)

    async def _aclose_between_attempts(_seconds):
        await client.aclose()

    with patch("libex_core.audible.client.httpx.AsyncClient", side_effect=_fake_async_client), \
         patch(
             "libex_core.audible.client.asyncio.sleep",
             new=AsyncMock(side_effect=_aclose_between_attempts),
         ):
        with pytest.raises(RuntimeError, match="has been closed"):
            await client.get("us", "/1.0/catalog/products")

    # Exactly one real request went out -- the second attempt refused before
    # ever reaching client.get(), so the retryable 429 never got a chance to
    # be retried into a third.
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_get_refuses_when_aclose_lands_while_parked_on_the_semaphore_permit(monkeypatch):
    """
    A third aclose race, distinct from the two above rather than a restatement
    of either: test_aclose_refuses_before_acquiring_a_semaphore_permit covers
    an instance already closed before get() is ever called, so the semaphore
    is never touched at all; test_get_refuses_on_the_next_attempt_after_aclose_mid_retry
    covers aclose() landing during the backoff sleep between two attempts,
    after that attempt's permit has already been released. This one is the
    window in between those two: a coroutine genuinely PARKED waiting to
    acquire the permit itself, under real contention, when aclose() runs
    concurrently on the same instance from another task.

    Needs real contention to open this window at all -- an uncontended
    semaphore acquires instantly, so the only permit is held here, by this
    test, for the whole time the client.get() task is parked waiting for it.

    The client is fetched only after the permit is granted, with no further
    await before the send (see get()'s own docstring), so a close landing
    during this wait must be caught by _get_client's own explicit closed
    check rather than the bare, undocumented RuntimeError httpx itself
    raises internally on a closed client. Both are plain RuntimeErrors with
    "closed" somewhere in the message, so asserting only the exception type,
    or matching a loose substring both messages share, would pass either
    way -- the assertion below pins the phrase that is unique to
    _get_client's own message, proving which of the two actually surfaced.
    """
    monkeypatch.setattr(client_module, "AUDIBLE_CONCURRENCY_LIMIT", 1)
    monkeypatch.setattr(client_module, "_audible_semaphore", None)
    monkeypatch.setattr(client_module, "_audible_semaphore_loop", None)

    client = _client()
    semaphore = client_module._get_audible_semaphore()
    await semaphore.acquire()  # the only permit -- get() below must genuinely park on it

    task = asyncio.create_task(client.get("us", "/1.0/catalog/products"))
    await asyncio.sleep(0)  # let the task run up to and block on the permit
    assert not task.done()  # confirms it actually parked rather than racing past this point

    await client.aclose()
    semaphore.release()  # unblocks the parked acquire, now that the instance is closed

    with pytest.raises(RuntimeError) as exc_info:
        await task

    assert "will not reopen" in str(exc_info.value)


# ============================================================
# __AENTER__ / __AEXIT__
# ============================================================

@pytest.mark.asyncio
async def test_aenter_builds_no_client():
    """Building a client in __aenter__ would open a second build path
    alongside _get_client's lazy one."""
    client = _client()
    async with client as entered:
        assert entered is client
        assert client.is_open is False


@pytest.mark.asyncio
async def test_aexit_calls_aclose():
    client = _client()
    async with client:
        client._get_client()
        assert client.is_open is True
    assert client.is_open is False
    assert client._closed is True


# ============================================================
# trust_env=False -- AN AMBIENT HTTPS_PROXY MUST NEVER REACH THE CLIENT
# ============================================================

@pytest.mark.asyncio
async def test_built_client_ignores_https_proxy_env_in_direct_mode(monkeypatch):
    """httpx.AsyncClient's own default (trust_env=True) reads HTTPS_PROXY and
    the SSL_CERT_* variables straight from the process environment -- that
    would make this instance's explicit direct/proxy choice only half the
    story. Checked against the client's own _mounts dict."""
    monkeypatch.setenv("HTTPS_PROXY", "http://should-be-ignored.example:9999")
    client = _client()

    http_client = client._get_client()
    try:
        assert http_client._mounts == {}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_built_client_mounts_the_configured_proxy_not_env(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://should-be-ignored.example:9999")
    client = _client(proxy_url="http://libex-one-vpn:8888", allow_direct_egress=False)

    http_client = client._get_client()
    try:
        assert http_client._mounts != {}
    finally:
        await client.aclose()


# ============================================================
# LATE-READ MODULE STATE (AUDIBLE_MAX_ATTEMPTS, _AUDIBLE_POOL_LIMITS)
# ============================================================

@pytest.mark.asyncio
async def test_audible_max_attempts_is_read_live_not_snapshotted_at_construction(monkeypatch):
    """
    Constructed BEFORE the monkeypatch, used AFTER it -- proving
    AUDIBLE_MAX_ATTEMPTS is read from module scope inside get()'s own retry
    loop at call time, not captured once in __init__. A constructor snapshot
    would pass this test while silently keeping the original value it
    captured, which is exactly the inert-test failure this guards against.
    """
    client = _client()
    monkeypatch.setattr(client_module, "AUDIBLE_MAX_ATTEMPTS", 5)

    get_mock = AsyncMock(return_value=_mock_response(429))
    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("libex_core.audible.client.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(AudibleAPIException):
            await client.get("us", "/1.0/catalog/products")

    assert get_mock.await_count == 5
    await client.aclose()


@pytest.mark.asyncio
async def test_audible_pool_limits_is_read_live_not_snapshotted_at_construction(monkeypatch):
    """
    Same late-read requirement for _AUDIBLE_POOL_LIMITS:
    scripts/refresh_corpus.py's _raise_process_limits() rebinds it directly
    on this module AFTER the hosted LibexClient instance already exists, so
    _get_client must read it fresh at build time rather than whatever was
    current when __init__ ran -- a constructor snapshot would silently cap a
    raised-limit run back down to the original value while the log line
    claims the new one.
    """
    client = _client()
    new_limits = httpx.Limits(max_connections=999, max_keepalive_connections=999, keepalive_expiry=5.0)
    monkeypatch.setattr(client_module, "_AUDIBLE_POOL_LIMITS", new_limits)

    captured = {}
    real_async_client_cls = httpx.AsyncClient

    def _capture(*args, **kwargs):
        captured["limits"] = kwargs.get("limits")
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        return real_async_client_cls(*args, **kwargs)

    with patch("libex_core.audible.client.httpx.AsyncClient", side_effect=_capture):
        client._get_client()

    assert captured["limits"] is new_limits
    await client.aclose()


# ============================================================
# THE SSRF MATRIX
# ============================================================

@pytest.mark.parametrize(
    "bad_path",
    [
        "@evil.example/x",
        ".evil.example/x",
        ":8080/x",
        "\\evil.example/x",
        "//evil.example/x",
    ],
)
def test_get_audible_url_rejects_ssrf_shaped_paths(bad_path):
    """A leading '@' turns the rest of the built string into userinfo and
    moves the host to whatever follows it; a leading '.' extends the
    intended host into a longer one the caller controls; a leading ':'
    overrides the port; a leading backslash is treated by some parsers as a
    path separator and by others as part of the host; a leading '//' is
    protocol-relative and replaces the host outright. All five are rejected
    before any URL is built from path at all."""
    with pytest.raises(ValueError) as exc_info:
        get_audible_url("us", bad_path)

    message = str(exc_info.value)
    # get_audible_url never receives params or extra_headers in the first
    # place, so neither can leak into this message even naming the path.
    assert "params" not in message
    assert "extra_headers" not in message


def test_get_audible_url_accepts_a_real_path():
    url = get_audible_url("us", "/1.0/catalog/products/B08G9PRS1K")
    assert url == "https://api.audible.com/1.0/catalog/products/B08G9PRS1K"


def test_get_audible_url_signature_has_no_params_or_headers_parameter():
    """Structural guarantee behind the message assertions above: this
    function cannot leak params or extra_headers into its ValueError message
    because it never receives them in the first place."""
    sig = inspect.signature(get_audible_url)
    assert set(sig.parameters) == {"region", "path"}


# ============================================================
# A REDIRECT IS SURFACED, NEVER FOLLOWED
# ============================================================

@pytest.mark.asyncio
async def test_a_302_is_surfaced_not_followed():
    call_count = {"n": 0}

    def _handler(request):
        call_count["n"] += 1
        return httpx.Response(302, headers={"Location": "https://evil.example/steal"})

    real_async_client_cls = httpx.AsyncClient

    def _fake_async_client(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async_client_cls(*args, **kwargs)

    client = _client()
    with patch("libex_core.audible.client.httpx.AsyncClient", side_effect=_fake_async_client):
        with pytest.raises(AudibleAPIException) as exc_info:
            await client.get("us", "/1.0/catalog/products")

    assert exc_info.value.upstream_status == 302
    assert call_count["n"] == 1  # never chased the redirect
    await client.aclose()


# ============================================================
# AUDIBLE_GET EXTRA_HEADERS OVERLAY
# ============================================================

@pytest.mark.asyncio
async def test_get_without_extra_headers_uses_region_headers_unchanged():
    """Omitting extra_headers (every pre-existing call site) leaves the
    headers byte-identical to get_region_headers' own output -- no stray
    keys added."""
    fixed_headers = {"User-Agent": "fixed", "X-ADP-SW": "12345678"}
    captured = {}

    async def _get(url, headers=None, params=None, timeout=None, follow_redirects=None):
        captured["headers"] = headers
        captured["follow_redirects"] = follow_redirects
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"ok": True}
        return response

    client = _client()
    with patch.object(client_module, "get_region_headers", return_value=fixed_headers), \
         patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        await client.get("us", "/1.0/catalog/products", {"page": 0})

    assert captured["headers"] == fixed_headers
    # a redirect on this outbound call would mean an attacker-chosen host
    # gets followed silently; this must stay pinned to False.
    assert captured["follow_redirects"] is False
    await client.aclose()


@pytest.mark.asyncio
async def test_get_extra_headers_overlays_region_headers():
    """extra_headers is overlaid on top of get_region_headers for that one
    call only, without dropping or mutating the base headers."""
    fixed_headers = {"User-Agent": "fixed", "X-ADP-SW": "12345678"}
    captured = {}

    async def _get(url, headers=None, params=None, timeout=None, follow_redirects=None):
        captured["headers"] = headers
        captured["follow_redirects"] = follow_redirects
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"ok": True}
        return response

    client = _client()
    with patch.object(client_module, "get_region_headers", return_value=fixed_headers), \
         patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        await client.get(
            "us",
            "/1.0/screens/audible-android-author-detail/B000APF21M",
            {"author_asin": "B000APF21M"},
            extra_headers={"X-Device-Type-Id": "A10KISP2GWF0E4"},
        )

    assert captured["headers"] == {
        "User-Agent": "fixed",
        "X-ADP-SW": "12345678",
        "X-Device-Type-Id": "A10KISP2GWF0E4",
    }
    # base headers untouched by the overlay
    assert fixed_headers == {"User-Agent": "fixed", "X-ADP-SW": "12345678"}
    assert captured["follow_redirects"] is False
    await client.aclose()


# ============================================================
# GET() ERROR MESSAGE
# ============================================================

@pytest.mark.asyncio
async def test_request_error_with_empty_message_includes_type():
    """An httpx.RequestError with an empty str() still produces a
    diagnosable message (the exception type + URL), not a blank one."""
    # httpx.ConnectError("") stringifies to "" -- the case that produced the
    # blank "Audible API request failed: " in the wild.
    failing = AsyncMock(side_effect=httpx.ConnectError(""))
    client = _client()
    with patch("httpx.AsyncClient.get", new=failing):
        with pytest.raises(AudibleAPIException) as exc:
            await client.get("us", "/1.0/catalog/products", {"page": 0})

    msg = str(exc.value)
    assert "ConnectError" in msg
    assert "request failed:" in msg
    assert msg.strip() != "Audible API request failed:"
    await client.aclose()


# ============================================================
# RETRY / BACKOFF
# ============================================================

@pytest.mark.asyncio
async def test_get_retries_429_then_succeeds():
    """A 429 is retried, not raised immediately, and the eventual 200 is
    returned once the retry succeeds."""
    responses = [_mock_response(429), _mock_response(200, json_body={"ok": True})]
    get_mock = AsyncMock(side_effect=responses)

    client = _client()
    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = await client.get("us", "/1.0/catalog/products", {"page": 0})

    assert result == {"ok": True}
    assert get_mock.await_count == 2
    mock_sleep.assert_awaited_once()
    await client.aclose()


@pytest.mark.asyncio
async def test_get_retries_5xx_then_succeeds():
    """A 5xx is retried the same way a 429 is."""
    responses = [_mock_response(502), _mock_response(200, json_body={"ok": True})]
    get_mock = AsyncMock(side_effect=responses)

    client = _client()
    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()):
        result = await client.get("us", "/1.0/catalog/products", {"page": 0})

    assert result == {"ok": True}
    assert get_mock.await_count == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_get_exhausts_retries_and_raises():
    """A 429 on every attempt is retried up to AUDIBLE_MAX_ATTEMPTS times
    total (1 initial + AUDIBLE_MAX_ATTEMPTS - 1 retries), then raises --
    never retried indefinitely."""
    get_mock = AsyncMock(return_value=_mock_response(429))

    client = _client()
    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(AudibleAPIException) as exc:
            await client.get("us", "/1.0/catalog/products", {"page": 0})

    assert get_mock.await_count == client_module.AUDIBLE_MAX_ATTEMPTS
    assert mock_sleep.await_count == client_module.AUDIBLE_MAX_ATTEMPTS - 1
    assert exc.value.upstream_status == 429
    await client.aclose()


@pytest.mark.asyncio
async def test_get_404_is_never_retried():
    """A 404 is terminal -- it must raise NotFoundException on the very
    first attempt, with no retry and no sleep at all."""
    get_mock = AsyncMock(return_value=_mock_response(404))

    client = _client()
    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(NotFoundException):
            await client.get("us", "/1.0/catalog/products/BADASIN000", {})

    assert get_mock.await_count == 1
    mock_sleep.assert_not_called()
    await client.aclose()


@pytest.mark.asyncio
async def test_get_other_4xx_not_retried():
    """Every 4xx other than 404 or 429 is a real answer too -- raised
    immediately on the first attempt, never retried."""
    get_mock = AsyncMock(return_value=_mock_response(400))

    client = _client()
    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(AudibleAPIException) as exc:
            await client.get("us", "/1.0/catalog/products", {"page": 0})

    assert get_mock.await_count == 1
    mock_sleep.assert_not_called()
    assert exc.value.upstream_status == 400
    await client.aclose()


@pytest.mark.asyncio
async def test_get_timeout_not_retried():
    """httpx.TimeoutException is deliberately NOT retried -- a single
    attempt, then AudibleAPIException, never a retry loop on a slow/hung
    connection."""
    get_mock = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    client = _client()
    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(AudibleAPIException) as exc:
            await client.get("us", "/1.0/catalog/products", {"page": 0})

    assert get_mock.await_count == 1
    mock_sleep.assert_not_called()
    assert exc.value.upstream_status is None
    await client.aclose()


@pytest.mark.asyncio
async def test_get_request_error_not_retried():
    """httpx.RequestError (connection failures) is deliberately NOT retried
    either -- a single attempt, then AudibleAPIException."""
    get_mock = AsyncMock(side_effect=httpx.ConnectError("refused"))

    client = _client()
    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(AudibleAPIException) as exc:
            await client.get("us", "/1.0/catalog/products", {"page": 0})

    assert get_mock.await_count == 1
    mock_sleep.assert_not_called()
    assert exc.value.upstream_status is None
    await client.aclose()


@pytest.mark.asyncio
async def test_get_honors_retry_after_numeric_seconds_over_computed_backoff():
    """A numeric Retry-After header wins outright over computed backoff --
    the sleep duration must equal the header's value, not a jittered
    exponential guess."""
    responses = [
        _mock_response(429, headers={"Retry-After": "3"}),
        _mock_response(200, json_body={"ok": True}),
    ]
    get_mock = AsyncMock(side_effect=responses)

    client = _client()
    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await client.get("us", "/1.0/catalog/products", {"page": 0})

    mock_sleep.assert_awaited_once_with(3.0)
    await client.aclose()


@pytest.mark.asyncio
async def test_get_retry_after_is_capped():
    """A Retry-After value beyond AUDIBLE_RETRY_AFTER_CAP_SECONDS is capped,
    not honored verbatim."""
    responses = [
        _mock_response(429, headers={"Retry-After": "9999"}),
        _mock_response(200, json_body={"ok": True}),
    ]
    get_mock = AsyncMock(side_effect=responses)

    client = _client()
    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await client.get("us", "/1.0/catalog/products", {"page": 0})

    mock_sleep.assert_awaited_once_with(client_module.AUDIBLE_RETRY_AFTER_CAP_SECONDS)
    await client.aclose()


@pytest.mark.asyncio
async def test_get_backoff_used_when_no_retry_after_header():
    """With no Retry-After header at all, the computed full-jitter backoff
    is used instead -- pinned here by patching random.uniform to identify
    the ceiling it was called with, rather than asserting an exact sleep
    duration (which is randomized by design)."""
    responses = [_mock_response(429), _mock_response(200, json_body={"ok": True})]
    get_mock = AsyncMock(side_effect=responses)

    client = _client()
    with patch("httpx.AsyncClient.get", new=get_mock), \
         patch("asyncio.sleep", new=AsyncMock()) as mock_sleep, \
         patch("random.uniform", return_value=0.0) as mock_uniform:
        await client.get("us", "/1.0/catalog/products", {"page": 0})

    mock_uniform.assert_called_once_with(0, client_module.AUDIBLE_RETRY_BASE_SECONDS)
    mock_sleep.assert_awaited_once_with(0.0)
    await client.aclose()


# ============================================================
# CONCURRENCY SEMAPHORE
# ============================================================

@pytest.mark.asyncio
async def test_get_audible_semaphore_bounds_in_flight_requests(monkeypatch):
    """The semaphore actually bounds how many callers can hold it at once to
    AUDIBLE_CONCURRENCY_LIMIT -- proven by driving real concurrency past the
    limit and watching the observed peak never exceed it."""
    monkeypatch.setattr(client_module, "AUDIBLE_CONCURRENCY_LIMIT", 2)
    monkeypatch.setattr(client_module, "_audible_semaphore", None)
    monkeypatch.setattr(client_module, "_audible_semaphore_loop", None)

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal in_flight, max_in_flight
        async with client_module._get_audible_semaphore():
            async with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            async with lock:
                in_flight -= 1

    await asyncio.gather(*(worker() for _ in range(8)))

    assert max_in_flight == 2


def test_get_audible_semaphore_reuses_instance_within_same_running_loop():
    """A second call inside the same running event loop must return the
    exact same Semaphore instance, not a fresh one -- otherwise waiters from
    the first instance would never see permits released via the second."""
    client_module._audible_semaphore = None
    client_module._audible_semaphore_loop = None

    async def get_two():
        first = client_module._get_audible_semaphore()
        second = client_module._get_audible_semaphore()
        return first, second

    loop = asyncio.new_event_loop()
    try:
        first, second = loop.run_until_complete(get_two())
    finally:
        loop.close()
        client_module._audible_semaphore = None
        client_module._audible_semaphore_loop = None

    assert first is second


def test_get_audible_semaphore_rekeys_when_running_loop_changes():
    """A fresh event loop must get a fresh Semaphore instance, not one bound
    to a now-closed loop's waiter state -- this is exactly what makes the
    module safe under pytest's own function-scoped event loops, each of
    which is a 'new running loop' from this function's point of view."""
    client_module._audible_semaphore = None
    client_module._audible_semaphore_loop = None

    async def get_sem():
        return client_module._get_audible_semaphore()

    loop1 = asyncio.new_event_loop()
    try:
        sem1 = loop1.run_until_complete(get_sem())
    finally:
        loop1.close()

    loop2 = asyncio.new_event_loop()
    try:
        sem2 = loop2.run_until_complete(get_sem())
    finally:
        loop2.close()
        client_module._audible_semaphore = None
        client_module._audible_semaphore_loop = None

    assert sem1 is not sem2


# ============================================================
# THE ONE TEST THAT MATTERS MOST
#
# Two LibexClient instances in the same process share one process-wide
# REQUEST budget -- the module-level semaphore -- so the SUM of their
# concurrent in-flight requests, not each instance's own, never exceeds
# AUDIBLE_CONCURRENCY_LIMIT. This is the invariant the whole per-instance
# slice is built around: two instances double-fanning-out against one exit
# IP is the failure that cost the VPN rotation once already, and the
# semaphores staying module-level rather than becoming instance state is
# what still prevents it now that LibexClient itself is no longer a
# singleton.
#
# Named for exactly what it proves and nothing more: a shared REQUEST
# budget. It does NOT prove the process's exit-IP CONNECTION footprint is
# bounded -- this claim was found overstated during review, since
# httpx.Limits is per-client configuration, not a shared budget (see
# _AUDIBLE_POOL_LIMITS' own module comment): two instances get two
# connection pools, so up to 2x as many sockets against one exit IP, with up
# to 2x as many able to sit idle for the full keepalive_expiry. That gap is
# real and undocumented nowhere else but here; this test does not close it,
# and must never be read as if it did.
# ============================================================

@pytest.mark.asyncio
async def test_two_instances_share_one_process_wide_request_budget(monkeypatch):
    monkeypatch.setattr(client_module, "AUDIBLE_CONCURRENCY_LIMIT", 3)
    monkeypatch.setattr(client_module, "_audible_semaphore", None)
    monkeypatch.setattr(client_module, "_audible_semaphore_loop", None)

    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def _handler(request):
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        async with lock:
            in_flight -= 1
        return httpx.Response(200, json={"ok": True})

    real_async_client_cls = httpx.AsyncClient

    def _fake_async_client(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async_client_cls(*args, **kwargs)

    client_a = _client()
    client_b = _client()

    with patch("libex_core.audible.client.httpx.AsyncClient", side_effect=_fake_async_client):
        await asyncio.gather(
            *(client_a.get("us", "/1.0/catalog/products") for _ in range(5)),
            *(client_b.get("us", "/1.0/catalog/products") for _ in range(5)),
        )

    # Under two independent, per-instance semaphores of the same limit, this
    # burst could reach 6 in flight at once (3 from each instance running
    # concurrently) -- this assertion is exactly what a mutation to instance
    # attributes fails.
    assert peak <= 3
    # And the shared budget is actually saturated across both instances
    # together, not merely never violated by luck.
    assert peak == 3

    await client_a.aclose()
    await client_b.aclose()


# ============================================================
# IMPORT-TIME ISOLATION -- A .ENV IN THE IMPORTING DIRECTORY READS NOTHING
# ============================================================

_ENV_DIR_CHILD_SCRIPT = """
from libex_core.audible.client import LibexClient

client = LibexClient(proxy_url=None, allow_direct_egress=True)
summary = client.transport_summary()
print("MODE:" + summary.mode)
print("HOST:" + (summary.host or ""))
"""


def test_constructing_a_client_from_a_dotenv_holding_directory_reads_nothing(tmp_path):
    """libex_core reads no environment and no settings of its own (see the
    module's own docstring) -- constructing a LibexClient from a working
    directory that happens to hold a .env naming AUDIBLE_PROXY_URL must not
    pick it up; only the proxy_url argument passed here decides. Run in a
    subprocess from that directory, the same isolation technique
    test_isolation.py uses, since only a genuinely fresh interpreter proves
    this rather than one that already has libex_core loaded from an earlier
    test."""
    (tmp_path / ".env").write_text(
        "AUDIBLE_PROXY_URL=http://should-never-be-read.example:8888\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", _ENV_DIR_CHILD_SCRIPT],
        cwd=tmp_path,
        env={"PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "MODE:direct" in result.stdout
    assert "should-never-be-read" not in result.stdout
