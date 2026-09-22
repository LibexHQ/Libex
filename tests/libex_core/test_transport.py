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
import shutil
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
    before any URL is built from path at all.

    Every payload here sits at character 0 of path, which is the whole reason
    the section below this one exists: this matrix exercises the leading-
    character check and nothing else."""
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
# THE SSRF MATRIX -- OFF THE FRONT OF THE PATH
# ============================================================

# The matrix above is position-blind, not merely short: every one of its five
# payloads sits at character 0 of path, so every one is caught by the
# leading-character check on its own, and nothing in this suite ever put a
# hostile segment anywhere else in a path. A payload that never moves off the
# front cannot tell a check that reads only the front from a check that reads
# the whole path, and that is the hole that shipped. Everything below puts the
# hostile segment somewhere the first check does not look.

_MID_PATH_DOT_SEGMENTS = [
    # After the fixed prefix every real call site starts with -- the shape a
    # crafted ASIN or id would actually arrive in.
    "/1.0/catalog/products/../../internal",
    "/1.0/catalog/products/../internal",
    "/1.0/catalog/products/./internal",
    "/1.0/content/../../internal/thing",
    # Past a legitimate-looking id, which is how one would survive a check
    # that only inspected the id's own first character.
    "/1.0/catalog/products/B0GS7CGR3Y/../../../internal",
    # A dot segment immediately after the leading "/": still not at character
    # 0, so still invisible to the leading-character check.
    "/../internal",
    "/./internal",
    # Trailing, with nothing after it to make it look like a traversal.
    "/1.0/catalog/products/..",
]


@pytest.mark.parametrize("bad_path", _MID_PATH_DOT_SEGMENTS)
def test_get_audible_url_rejects_dot_segments_away_from_the_front(bad_path):
    """A "." or ".." segment is rejected wherever it falls in path, not only
    at the front. httpx applies RFC 3986 dot-segment removal when it parses
    the URL get_audible_url builds, so one of these placed after a fixed
    prefix collapses onto an endpoint the function never meant to address --
    and it does that while leaving host, scheme and port untouched, which is
    all the post-build check looks at, so that check cannot see it either.
    Neither of the two checks that existed before this one can catch these:
    the payload is not at character 0, and the finished URL still points at
    the right host on the right port."""
    with pytest.raises(ValueError) as exc_info:
        get_audible_url("us", bad_path)

    message = str(exc_info.value)
    assert bad_path in message
    # get_audible_url never receives params or extra_headers in the first
    # place, so neither can leak into this message even naming the path.
    assert "params" not in message
    assert "extra_headers" not in message


def test_a_mid_path_dot_segment_would_collapse_with_host_scheme_and_port_intact():
    """The reason the post-build host/scheme/port check is not enough, shown
    rather than asserted in prose: parse the URL get_audible_url would have
    built from a mid-path traversal and the path has already collapsed --
    /1.0/catalog/products/../../internal becomes /1.0/internal -- while host,
    scheme and port are exactly what the function intended. A check reading
    only those three sees a perfectly well-formed request to Audible."""
    parsed = httpx.URL("https://api.audible.com/1.0/catalog/products/../../internal")

    assert parsed.raw_path == b"/1.0/internal"
    assert parsed.host == "api.audible.com"
    assert parsed.scheme == "https"
    assert parsed.port is None


_ENCODED_DOT_SEGMENTS = [
    "/1.0/catalog/products/..%2F..%2Finternal",
    "/1.0/catalog/products/..%2f..%2finternal",
    "/%2e%2e/%2e%2e/internal",
    "/%2E%2E/internal",
    "/%2e/internal",
    "/1.0/catalog/products/B000000000%2Fmalicious",
]


@pytest.mark.parametrize("bad_path", _ENCODED_DOT_SEGMENTS)
def test_get_audible_url_rejects_percent_encoded_dot_segments(bad_path):
    """Percent-encoded spellings are rejected too, in either case, and so is
    any segment carrying an encoded separator.

    These are not rejected because httpx acts on them -- it does not.
    httpx.URL(...).raw_path, the bytes that actually go on the wire, keeps the
    encoding literal, so httpx never turns %2F into a separator and never
    collapses %2e%2e (see the test below, which pins both halves of that).
    They are rejected because httpx.URL(...).path -- the decoded view --
    resolves them to the same traversal the literal ".." spells out, and
    whether Audible's own edge decodes before it routes is third-party
    infrastructure this module cannot see or test. A server that decodes then
    routes lands exactly where the unencoded form would have.

    What makes taking the safe reading obvious rather than paranoid: no call
    site anywhere in this codebase produces a percent-encoded path segment --
    every one interpolates a bare ASIN or ISBN-keyed id -- so rejecting these
    costs no real request. Delete these cases and the only thing recovered is
    the ability to send a path nothing here ever builds."""
    with pytest.raises(ValueError) as exc_info:
        get_audible_url("us", bad_path)

    message = str(exc_info.value)
    assert bad_path in message
    assert "params" not in message
    assert "extra_headers" not in message


def test_encoding_is_inert_to_httpx_but_not_to_a_decode_then_route_server():
    """Both halves of the reasoning above, pinned, so a future reader can see
    why the encoded cases are rejected on top of the literal one instead of
    guessing: raw_path -- what leaves this process -- keeps %2F and %2e
    exactly as written, while path -- what a server decoding before routing
    effectively sees -- is the traversal. The first assertion is why httpx
    alone cannot be relied on to have made these harmless; the second is why
    they are worth refusing anyway."""
    parsed = httpx.URL("https://api.audible.com/1.0/catalog/products/..%2F..%2Finternal")
    assert parsed.raw_path == b"/1.0/catalog/products/..%2F..%2Finternal"
    assert parsed.path == "/1.0/catalog/products/../../internal"

    encoded_dots = httpx.URL("https://api.audible.com/%2e%2e/%2e%2e/internal")
    assert encoded_dots.raw_path == b"/%2e%2e/%2e%2e/internal"
    assert encoded_dots.path == "/../../internal"


# ============================================================
# THE ACCEPTANCE MATRIX -- EVERY LIVE TEMPLATE, EVERY REGION
# ============================================================

# A path guard fails dangerously by over-rejecting: a legitimate call it
# refuses is an endpoint that silently stops working in one region, for one
# id format, long after the guard was written. One template in one region --
# which is all this file pinned before -- cannot show that.
#
# Every template below is one the codebase actually sends, with the call
# sites that send it named, so a template that stops being real can be
# removed on evidence rather than on a guess:
_FIXED_PATHS = (
    # app/services/audible/search.py:89, app/services/audible/releases.py:261
    "/1.0/catalog/products/",
    # app/services/audible/books.py:697, app/services/seeder.py:472,
    # app/services/audible/series.py:254, authors/catalog.py:44 and :555.
    # Both spellings are live; the trailing slash is not interchangeable
    # noise to a guard that splits on "/", since it yields a final empty
    # segment.
    "/1.0/catalog/products",
    # app/services/audible/search.py:163, authors/__init__.py:1007
    "/1.0/searchsuggestions",
    # app/services/audible/releases.py:122, app/services/seeder.py:564
    "/1.0/catalog/categories",
)

_ID_TEMPLATES = (
    # app/services/audible/books.py:689, series.py:83 and :189, seeder.py:376
    "/1.0/catalog/products/{book_id}",
    # app/services/audible/authors/__init__.py:203
    "/1.0/catalog/contributors/{book_id}",
    # app/services/audible/authors/screens.py:701
    "/1.0/screens/audible-android-author-detail/{book_id}",
    # app/services/audible/books.py:1161 and :1247,
    # scripts/backfill_chapters.py:187
    "/1.0/content/{book_id}/metadata",
)

# Libex ids are not all B-format -- ISBN-keyed records exist alongside them,
# and a guard that assumed B-format would sail through a B-only matrix.
_LIVE_IDS = ("B0GS7CGR3Y", "0008433844", "B08G9PRS1K")

_LIVE_PATHS = list(_FIXED_PATHS) + [
    template.format(book_id=book_id)
    for template in _ID_TEMPLATES
    for book_id in _LIVE_IDS
]

# Written out rather than read from REGION_MAP on purpose: taking the hosts
# from the same table get_audible_url uses would make this assert only that
# the function agrees with itself. A typo'd tld here fails the test; a typo'd
# tld in REGION_MAP fails it too.
_EXPECTED_HOSTS = {
    "us": "api.audible.com",
    "uk": "api.audible.co.uk",
    "ca": "api.audible.ca",
    "au": "api.audible.com.au",
    "de": "api.audible.de",
    "fr": "api.audible.fr",
    "it": "api.audible.it",
    "es": "api.audible.es",
    "jp": "api.audible.co.jp",
    "in": "api.audible.in",
    "br": "api.audible.com.br",
}


def test_the_acceptance_matrix_covers_every_region_the_module_accepts():
    """The matrix below is only a non-regression guarantee for the regions it
    actually lists, so a twelfth region added to the module without a row here
    would be guarded by nothing. This fails the moment those two drift."""
    assert set(_EXPECTED_HOSTS) == client_module.VALID_REGIONS


@pytest.mark.parametrize("region", sorted(_EXPECTED_HOSTS))
@pytest.mark.parametrize("path", _LIVE_PATHS)
def test_get_audible_url_accepts_every_live_path_in_every_region(region, path):
    """Every real path template the codebase sends, in all eleven regions,
    across a B-format and an ISBN-keyed id. None of them may be rejected, and
    each must build the exact URL expected -- host and path both, with the
    path reaching the wire byte-for-byte as handed in rather than collapsed or
    re-encoded on the way."""
    url = get_audible_url(region, path)

    assert url == f"https://{_EXPECTED_HOSTS[region]}{path}"
    assert httpx.URL(url).raw_path == path.encode()


def test_the_acceptance_matrix_has_not_shrunk():
    """Guards the matrix itself against quietly losing rows: 16 live paths
    across 11 regions is 176 combinations, and a template or an id dropped
    from the lists above would shrink that silently while every remaining case
    still passed."""
    assert len(_LIVE_PATHS) == 16
    assert len(_LIVE_PATHS) * len(_EXPECTED_HOSTS) == 176


# ============================================================
# THE SSRF MATRIX -- PAST A PATH TERMINATOR, IN EVERY REGION
# ============================================================

# This section sits below the acceptance matrix rather than beside the two
# rejection matrices it continues, because it reuses that matrix's
# hand-written region list: every case here runs in all eleven regions. The
# guard runs before the region is looked up at all, so a us-only rejection
# test cannot tell a region-blind guard from one that happens to be reached
# only on the region the test picked.
#
# What the two matrices above missed is that "/" is not the only thing that
# ends a path segment. "?" and "#" each terminate the path component
# outright, so a dot segment immediately in front of one is the path's final
# segment in the bytes transmitted while a split on "/" never sees a "." or
# ".." segment at all. The payloads below are exactly that shape.

_TERMINATED_DOT_SEGMENTS = [
    # ".." in front of each terminator, bare and with something after it.
    "/1.0/catalog/products/..?",
    "/1.0/catalog/products/..?x",
    "/1.0/catalog/products/..#",
    "/1.0/catalog/products/..#x",
    # "." in front of each. It collapses to a shorter path rather than a
    # higher directory, so it is the quieter of the two and the one a guard
    # written against ".." alone would let through.
    "/1.0/catalog/products/.?",
    "/1.0/catalog/products/.?x",
    "/1.0/catalog/products/.#",
    "/1.0/catalog/products/.#x",
]


@pytest.mark.parametrize("region", sorted(_EXPECTED_HOSTS))
@pytest.mark.parametrize("bad_path", _TERMINATED_DOT_SEGMENTS)
def test_get_audible_url_rejects_dot_segments_in_front_of_a_terminator(region, bad_path):
    """A dot segment in front of "?" or "#" is rejected in every region.

    This is a stronger case than the percent-encoded matrix above, not a
    variation on it: the encoded spellings are refused on what a third-party
    server might do after decoding, while these are the bytes that leave the
    process. httpx collapses them itself, before the request is sent, exactly
    as it collapses the between-slashes form -- see the raw_path table below,
    which is what these paths transmitted as before the guard covered them.
    Host, scheme and port do not move, so the post-build check cannot see any
    of it."""
    with pytest.raises(ValueError) as exc_info:
        get_audible_url(region, bad_path)

    message = str(exc_info.value)
    assert bad_path in message
    # get_audible_url never receives params or extra_headers in the first
    # place, so neither can leak into this message even naming the path.
    assert "params" not in message
    assert "extra_headers" not in message


def test_a_terminator_hides_a_trailing_dot_segment_from_a_split_on_slash():
    """The harm, measured rather than described, for all eight payloads
    above: what httpx would put on the wire if the guard let them through.
    Every one of them addresses something other than what the path spells
    out, and four of them address a directory above the prefix the caller
    asked for, while the split-on-"/" view of the same string contains no
    "." or ".." segment anywhere.

    The second half of each assertion is the part that matters: these are
    raw_path, the transmitted bytes, not the decoded .path view the encoded
    cases rest on."""
    collapsed = {
        path: httpx.URL(f"https://api.audible.com{path}").raw_path
        for path in _TERMINATED_DOT_SEGMENTS
    }

    assert collapsed == {
        "/1.0/catalog/products/..?": b"/1.0/catalog?",
        "/1.0/catalog/products/..?x": b"/1.0/catalog?x",
        "/1.0/catalog/products/..#": b"/1.0/catalog",
        "/1.0/catalog/products/..#x": b"/1.0/catalog",
        "/1.0/catalog/products/.?": b"/1.0/catalog/products?",
        "/1.0/catalog/products/.?x": b"/1.0/catalog/products?x",
        "/1.0/catalog/products/.#": b"/1.0/catalog/products",
        "/1.0/catalog/products/.#x": b"/1.0/catalog/products",
    }
    # And none of the eight contains a "." or ".." segment to find.
    for path in _TERMINATED_DOT_SEGMENTS:
        assert "." not in path.split("/")
        assert ".." not in path.split("/")


_BARE_TERMINATORS = [
    "/1.0/catalog/products?x=1",
    "/1.0/catalog/products#frag",
    "/1.0/searchsuggestions?keywords=a",
]


@pytest.mark.parametrize("region", sorted(_EXPECTED_HOSTS))
@pytest.mark.parametrize("bad_path", _BARE_TERMINATORS)
def test_get_audible_url_rejects_a_terminator_with_no_dot_segment_at_all(region, bad_path):
    """The two characters are refused, not the eight spellings of a dot
    segment in front of them -- and this is the test that pins that, because
    nothing else here can tell the two apart.

    Every payload here is innocuous on its face: a plausible query string on
    a template the codebase really sends, and a fragment. Without this test,
    someone re-permits "?" so a caller can inline a query string, every case
    in the matrix above still passes, and the collapse comes back with it.
    Rejecting these costs nothing: query parameters reach Audible through
    LibexClient.get's own params argument, which httpx appends to the URL
    this function returns, and a fragment is never sent to a server at all.

    If a future change genuinely needs a "?" in a path string, this test is
    the one that has to be argued with first."""
    with pytest.raises(ValueError) as exc_info:
        get_audible_url(region, bad_path)

    assert bad_path in str(exc_info.value)


_ENCODED_BACKSLASH_SEGMENTS = [
    "/1.0/catalog/products/..%5c..",
    "/1.0/catalog/products/..%5C..",
]


@pytest.mark.parametrize("region", sorted(_EXPECTED_HOSTS))
@pytest.mark.parametrize("bad_path", _ENCODED_BACKSLASH_SEGMENTS)
def test_get_audible_url_rejects_the_encoded_backslash_like_the_encoded_slash(region, bad_path):
    """%5c is the encoded backslash, and it is refused in either case on the
    same footing as %2f: a server that decodes before it routes lands where
    the literal form would have.

    What makes the pairing non-arbitrary rather than one more spelling added
    to a list: the literal backslash is already rejected by the leading-
    character check above, precisely because some parsers read it as a path
    separator. A guard that refused %2f but passed %5c would be resting on
    which byte an encoded separator happens to be spelled with.

    Note these are rejected by the encoded-separator check, not by the
    terminator check -- neither payload contains "?" or "#", which is what
    keeps the two mutation tests below independent of each other."""
    with pytest.raises(ValueError) as exc_info:
        get_audible_url(region, bad_path)

    assert bad_path in str(exc_info.value)


# ============================================================
# THE SCOPE LINE -- WHAT THIS GUARD DELIBERATELY DOES NOT CHASE
# ============================================================

# Pinned from the accepting side, which is the only side that can hold a
# line. Every test above adds something to what the guard refuses; nothing
# above records where refusing stops.

_OUT_OF_SCOPE_ENCODINGS = [
    # Double-encoded: "%252e" decodes to "%2e", which decodes to ".". Only a
    # server that decodes twice before routing sees a dot segment.
    "/1.0/catalog/products/%252e%252e/internal",
    "/%252e%252e/%252e%252e/internal",
    # Overlong UTF-8 for "/": invalid encoding that some decoders
    # historically accepted as a separator.
    "/1.0/catalog/products/..%c0%af..",
    "/1.0/catalog/products/..%c0%af../internal",
]


@pytest.mark.parametrize("region", sorted(_EXPECTED_HOSTS))
@pytest.mark.parametrize("path", _OUT_OF_SCOPE_ENCODINGS)
def test_get_audible_url_still_accepts_the_encodings_the_guard_does_not_chase(region, path):
    """These payloads are accepted today, and this test exists so that fact
    is a decision on the record rather than an accident nobody noticed.

    It is not an endorsement of the strings themselves. Matching every
    encoding of a dot segment is an arms race with no end, and the guard
    deliberately stops at the single-byte spellings -- the reasoning is in
    _has_unsafe_path_segment's own comment and is not repeated here. The
    reason to pin the stopping point now, when a previous pass deliberately
    asserted nothing in either direction about these forms, is that the guard
    has since grown to reject two whole characters. That makes "finish the
    job" a much more tempting next edit, and an untested scope line is one a
    future reader can cross without ever realising there was a line.

    So: widening the guard to catch these may well be right one day. This
    test does not forbid it. It makes doing it a visible, argued change --
    the test goes red, someone reads this docstring, and the widening is
    recorded -- instead of a silent drift into chasing encodings.

    The byte-for-byte assertion is the second half: accepted has to mean the
    path reaches the wire as handed in, not re-encoded or collapsed on the
    way through."""
    url = get_audible_url(region, path)

    assert url == f"https://{_EXPECTED_HOSTS[region]}{path}"
    assert httpx.URL(url).raw_path == path.encode()


# ============================================================
# THE GUARD IS NOT INERT -- DELETE IT AND THE TRAVERSAL BUILDS
# ============================================================

_GUARD_CALL_SOURCE = "    if _has_unsafe_path_segment(path):"
_GUARD_CALL_NEUTERED = "    if False:"

# The two checks inside _has_unsafe_path_segment that the sections above
# added, each mutated on its own so a test cannot be satisfied by the other
# check happening to catch the same payload.
_TERMINATOR_CHECK_SOURCE = '    if "?" in path or "#" in path:'
_TERMINATOR_CHECK_NEUTERED = "    if False:"
_ENCODED_SEPARATOR_CHECK_SOURCE = '        if "%2f" in lowered or "%5c" in lowered:'
_ENCODED_SEPARATOR_CHECK_WITHOUT_BACKSLASH = '        if "%2f" in lowered:'

_TRAVERSAL_CHILD_SCRIPT = """
from libex_core.audible.client import get_audible_url

try:
    print("BUILT:" + get_audible_url("us", "/1.0/catalog/products/../../internal"))
except ValueError as exc:
    print("REJECTED:" + str(exc))
"""


def _run_against_source_copy(package_dir):
    """Runs the traversal script against a copy of libex_core in its own
    directory, in a fresh interpreter, the same subprocess isolation
    test_isolation.py uses. The copy is what makes this safe to run at all:
    the guard is removed from a throwaway tree under tmp_path and never from
    the working tree, because a source file that is briefly broken on disk is
    broken for everything else reading the repo at that moment."""
    result = subprocess.run(
        [sys.executable, "-c", _TRAVERSAL_CHILD_SCRIPT],
        cwd=package_dir,
        env={"PYTHONPATH": str(package_dir)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


_PATH_PROBE_CHILD_SCRIPT = """
import sys

from libex_core.audible.client import get_audible_url

for path in sys.argv[1:]:
    try:
        get_audible_url("us", path)
        print("BUILT:" + path)
    except ValueError:
        print("REJECTED:" + path)
"""


def _probe_paths_against_source_copy(package_dir, paths):
    """Same subprocess isolation as _run_against_source_copy, for a whole
    list of paths at once: returns {path: "BUILT" | "REJECTED"}. Asking for
    the verdict on every payload in one child run is what lets the two
    mutation tests below assert that a mutation flips its own cases and
    leaves the other check's cases refused."""
    result = subprocess.run(
        [sys.executable, "-c", _PATH_PROBE_CHILD_SCRIPT, *paths],
        cwd=package_dir,
        env={"PYTHONPATH": str(package_dir)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr

    verdicts = {}
    for line in result.stdout.splitlines():
        verdict, path = line.split(":", 1)
        verdicts[path] = verdict
    assert set(verdicts) == set(paths), result.stdout
    return verdicts


def _copy_libex_core_with_replacement(tmp_path, name, original, replacement):
    """Copies libex_core into its own directory under tmp_path with one
    source line replaced, and refuses to proceed unless that line is present
    in the working tree exactly once.

    The copy is what makes any of this safe to run: the mutation lands in a
    throwaway tree and never in the working tree. The working tree is shared
    state -- a concurrent test run, an editor, a build all read the file as it
    stands on disk -- so a file that is deliberately wrong, even for the
    moment it takes to restore it, is wrong for all of them. Somewhere
    disposable is the only right place to break one on purpose. The count
    assertion is the other half -- it is what stops a mutation test from
    passing happily against a file that no longer contains the check it
    believes it is removing."""
    root = tmp_path / name
    shutil.copytree(REPO_ROOT / "libex_core", root / "libex_core")

    target = root / "libex_core" / "audible" / "client.py"
    source = target.read_text()
    assert source.count(original) == 1, (
        f"the source line this test mutates is no longer present as {original!r} "
        "-- update this test to match the code, do not delete it"
    )
    target.write_text(source.replace(original, replacement))
    return root


def test_removing_the_guard_makes_the_traversal_build(tmp_path):
    """Proof the tests above are not inert. A test that passes against the
    broken version protects nothing, so this runs the same traversal twice:
    once against a faithful copy of libex_core, where it must be rejected, and
    once against a copy with the guard call neutered, where it must build a
    URL. If only the first ran, a deleted guard would look identical to a
    working one.

    Deliberately not a mock: the thing under test is a two-line source-level
    check inside get_audible_url, and a mock of it would prove only that the
    mock was called. Only real source with the real check missing shows what
    its absence costs."""
    pristine = tmp_path / "pristine"
    shutil.copytree(REPO_ROOT / "libex_core", pristine / "libex_core")
    neutered = _copy_libex_core_with_replacement(
        tmp_path, "neutered", _GUARD_CALL_SOURCE, _GUARD_CALL_NEUTERED
    )

    pristine_output = _run_against_source_copy(pristine)
    neutered_output = _run_against_source_copy(neutered)

    assert pristine_output.startswith("REJECTED:")
    assert "/1.0/catalog/products/../../internal" in pristine_output
    # And with the guard gone the path is not merely accepted -- httpx has
    # already collapsed it onto an endpoint nothing here meant to call, which
    # is the concrete harm the guard prevents.
    assert neutered_output.startswith("BUILT:")
    assert httpx.URL(neutered_output.split("BUILT:", 1)[1].strip()).raw_path == b"/1.0/internal"


def test_the_working_tree_guard_is_the_one_the_suite_ran_against():
    """The copies above are only evidence about the real module if the real
    module still contains the guard they copied. Cheap, and it closes the gap
    where someone deletes the guard from the working tree and the mutation
    test above goes on passing happily against two copies of a file that no
    longer has it."""
    source = (REPO_ROOT / "libex_core" / "audible" / "client.py").read_text()
    assert source.count(_GUARD_CALL_SOURCE) == 1


def test_removing_the_terminator_check_lets_every_terminator_case_build(tmp_path):
    """Proof the terminator tests above are not inert, and that they are
    carried by the "?"/"#" check rather than by something else in the guard
    that happens to catch the same strings.

    One mutation, three claims: with that one line neutered all eight dot-
    segment-past-a-terminator payloads build, all three bare query/fragment
    payloads build, and the two encoded-backslash payloads are still refused.
    The last of those is what makes this a test of one check instead of a
    test of the guard in general."""
    mutated = _copy_libex_core_with_replacement(
        tmp_path,
        "no_terminator_check",
        _TERMINATOR_CHECK_SOURCE,
        _TERMINATOR_CHECK_NEUTERED,
    )

    verdicts = _probe_paths_against_source_copy(
        mutated,
        _TERMINATED_DOT_SEGMENTS + _BARE_TERMINATORS + _ENCODED_BACKSLASH_SEGMENTS,
    )

    assert all(verdicts[path] == "BUILT" for path in _TERMINATED_DOT_SEGMENTS)
    assert all(verdicts[path] == "BUILT" for path in _BARE_TERMINATORS)
    assert all(verdicts[path] == "REJECTED" for path in _ENCODED_BACKSLASH_SEGMENTS)


def test_dropping_the_encoded_backslash_lets_only_its_own_cases_build(tmp_path):
    """The other half, mutated the way the check would realistically
    regress: %5c dropped from the encoded-separator condition while %2f stays.

    Both encoded-backslash payloads then build and nothing else moves --
    the terminator cases stay refused, and so do the encoded-slash cases,
    which is what shows the two spellings are genuinely independent branches
    rather than one condition covering for the other."""
    mutated = _copy_libex_core_with_replacement(
        tmp_path,
        "no_encoded_backslash",
        _ENCODED_SEPARATOR_CHECK_SOURCE,
        _ENCODED_SEPARATOR_CHECK_WITHOUT_BACKSLASH,
    )

    verdicts = _probe_paths_against_source_copy(
        mutated,
        _ENCODED_BACKSLASH_SEGMENTS + _TERMINATED_DOT_SEGMENTS + _ENCODED_DOT_SEGMENTS,
    )

    assert all(verdicts[path] == "BUILT" for path in _ENCODED_BACKSLASH_SEGMENTS)
    assert all(verdicts[path] == "REJECTED" for path in _TERMINATED_DOT_SEGMENTS)
    assert all(verdicts[path] == "REJECTED" for path in _ENCODED_DOT_SEGMENTS)


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
