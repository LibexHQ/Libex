"""
libex_core.audible.client's transport contract: how egress is configured,
what it validates, what it exposes, and how a request in flight is affected
by a reconfigure landing mid-retry.

This module carries no dependency on the hosted application and no
dependency on Postgres or a real network -- see test_isolation.py for the
subprocess/AST proof of the first, and tests/conftest.py's autouse
block_network_sockets fixture for the second, which applies here exactly as
it does everywhere else in the suite. Every test below that builds a real
httpx.AsyncClient answers it with an in-process httpx.MockTransport rather
than a mocked .get(), specifically where the real closed-client check inside
AsyncClient.send() is the thing being proved.
"""

# Standard library
import asyncio
import dataclasses
import subprocess
import sys
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Third party
import httpx
import pytest

# Local
import libex_core.audible.client as audible_client

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def restore_transport():
    """
    Every test below configures the module's transport directly, which is
    otherwise set exactly once per process (at app.services.audible's own
    import time) -- without a restore, a test here would leak its configured
    state into whatever test runs after it, in this file or any other that
    shares the process. Snapshotting and restoring the private fields
    directly, rather than calling configure_transport() again with whatever
    the original value looked like, restores the exact prior snapshot object
    and generation counter rather than merely an equivalent one.
    """
    transport = audible_client._transport
    generation = audible_client._transport_generation
    client = audible_client._audible_client
    client_loop = audible_client._audible_client_loop
    client_generation = audible_client._audible_client_generation
    yield
    audible_client._transport = transport
    audible_client._transport_generation = generation
    audible_client._audible_client = client
    audible_client._audible_client_loop = client_loop
    audible_client._audible_client_generation = client_generation


# ============================================================
# trust_env=False -- an ambient HTTPS_PROXY must never reach the client
# ============================================================

@pytest.mark.asyncio
async def test_built_client_ignores_https_proxy_env_when_nothing_configured(
    monkeypatch, restore_transport,
):
    """httpx.AsyncClient's own default (trust_env=True) reads HTTPS_PROXY and
    the SSL_CERT_* variables straight from the process environment -- that
    would make configure_transport's explicit direct/proxy choice only half
    the story. Checked against the client's own _mounts dict: trust_env=True
    with HTTPS_PROXY set mounts a proxy transport for the https:// pattern;
    trust_env=False leaves it empty, confirmed directly against the
    installed httpx (not merely a documented default) since a mount stored
    on the client, not a request ever actually sent, is what is being proved
    here."""
    monkeypatch.setenv("HTTPS_PROXY", "http://should-be-ignored.example:9999")
    audible_client.configure_transport(None, allow_direct_egress=True)
    audible_client._audible_client = None
    audible_client._audible_client_loop = None
    audible_client._audible_client_generation = None

    client = audible_client._get_audible_client()
    try:
        assert client._mounts == {}
    finally:
        await client.aclose()


# ============================================================
# EAGER VALIDATION -- a malformed value raises, and names none of itself
#
# The messages below say "proxy URL", never "AUDIBLE_PROXY_URL" -- that env
# var is a hosted-application name, and this package never reads the
# environment or constructs Settings (see test_isolation.py and the
# .env-holding-directory test further down, which prove exactly that).
# configure_transport() takes a plain string from whatever caller passes it
# in, so a message naming AUDIBLE_PROXY_URL would assert knowledge of an
# environment variable an embedder driving this from a config file or a CLI
# flag never set. Pinned to the exact text on purpose: a substring or regex
# match on "proxy URL" would have passed straight through the prefix this
# test exists to keep out.
# ============================================================

@pytest.mark.parametrize(
    "case, value, expected_message",
    [
        (
            "missing scheme",
            "libex-seeder-vpn:8888",
            "proxy URL could not be parsed",
        ),
        (
            "bad scheme",
            "socks5://libex-seeder-vpn:8888",
            "proxy URL must use the http or https scheme",
        ),
        (
            "empty host",
            "http://:8888",
            "proxy URL must include a host and a valid port",
        ),
        (
            "bad port",
            "http://libex-seeder-vpn:999999",
            "proxy URL must include a host and a valid port",
        ),
    ],
)
def test_configure_transport_raises_on_malformed_value(
    case, value, expected_message, restore_transport,
):
    with pytest.raises(ValueError) as exc_info:
        audible_client.configure_transport(value)
    assert str(exc_info.value) == expected_message


def test_configure_transport_error_never_names_the_credentialed_value(restore_transport):
    """A scheme-less or otherwise malformed value can carry embedded
    credentials (user:pass@host:port) -- neither the raised message nor the
    traceback a caller would actually see (via `from None`, which suppresses
    the chained original httpx exception from ever being formatted in) may
    contain any part of it. Live-reproduced against the pinned httpx: a bad
    port after a credentialed authority raises httpx's own InvalidURL first,
    which is exactly the case `from None` exists to scrub."""
    credentialed = "http://opsuser:s3cr3t-token@libex-seeder-vpn:notaport"

    with pytest.raises(ValueError) as exc_info:
        audible_client.configure_transport(credentialed)

    exc = exc_info.value
    formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert "s3cr3t-token" not in str(exc)
    assert "opsuser" not in str(exc)
    assert "s3cr3t-token" not in formatted
    assert "opsuser" not in formatted
    # from None: no chained cause reaches a caller that formats this normally.
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True


# ============================================================
# None / "" -- REFUSED UNLESS allow_direct_egress=True IS PASSED,
# WHICH THEN CONFIGURES DIRECT EGRESS EXACTLY AS BEFORE
#
# A blank proxy_url alone is indistinguishable, at the wire, from an
# embedder who meant to configure a proxy and left the setting empty --
# see configure_transport's own docstring. Bare None/"" now raise instead
# of silently resolving to direct egress; allow_direct_egress=True is the
# explicit statement that direct egress really is what's wanted, and it
# reproduces the old bare-None/"" behaviour exactly. A real proxy URL is
# unaffected by the flag either way -- the last test below proves that.
# ============================================================

def test_none_without_allow_direct_egress_raises_value_error(restore_transport):
    with pytest.raises(ValueError) as exc_info:
        audible_client.configure_transport(None)
    assert "allow_direct_egress" in str(exc_info.value)


def test_empty_string_without_allow_direct_egress_raises_value_error(restore_transport):
    with pytest.raises(ValueError) as exc_info:
        audible_client.configure_transport("")
    assert "allow_direct_egress" in str(exc_info.value)


def test_none_without_allow_direct_egress_message_names_no_proxy_value(restore_transport):
    """The message is fixed text, never built from proxy_url -- there's
    nothing to leak from a blank value, but pinning this keeps the message
    from silently growing one later without a test noticing."""
    with pytest.raises(ValueError) as exc_info:
        audible_client.configure_transport(None)
    assert str(exc_info.value) == (
        "a blank or missing proxy URL no longer configures direct "
        "egress by itself -- pass allow_direct_egress=True to "
        "configure_transport() if egressing to Audible without a "
        "proxy is really what's intended, so a proxy setting that "
        "came through empty by mistake fails loudly instead of "
        "sending every request out on this process's own IP"
    )


def test_none_with_allow_direct_egress_configures_direct_egress(restore_transport):
    audible_client.configure_transport(None, allow_direct_egress=True)
    summary = audible_client.transport_summary()
    assert summary.mode == "direct"
    assert summary.host is None


def test_empty_string_with_allow_direct_egress_configures_direct_egress(restore_transport):
    audible_client.configure_transport("", allow_direct_egress=True)
    summary = audible_client.transport_summary()
    assert summary.mode == "direct"
    assert summary.host is None


def test_a_real_proxy_url_succeeds_regardless_of_allow_direct_egress(restore_transport):
    """Proves the flag is only ever consulted in the blank branch -- a
    supplied proxy URL configures proxy mode whether allow_direct_egress is
    left at its False default or passed explicitly."""
    audible_client.configure_transport(
        "http://libex-one-vpn:8888", allow_direct_egress=False
    )
    summary = audible_client.transport_summary()
    assert summary.mode == "proxy"
    assert summary.host == "libex-one-vpn"


# ============================================================
# transport_summary() -- NEVER THE URL, NEVER CREDENTIALS
# ============================================================

def test_transport_summary_carries_only_mode_and_host(restore_transport):
    """Pinned against the dataclass's own field list rather than merely
    checking hasattr for 'proxy' or 'url' -- a future field added to
    TransportSummary under some other name would still be caught here."""
    audible_client.configure_transport("http://opsuser:s3cr3t-token@libex-seeder-vpn:8888")
    summary = audible_client.transport_summary()

    field_names = {f.name for f in dataclasses.fields(summary)}
    assert field_names == {"mode", "host"}


def test_transport_summary_never_exposes_the_url_or_credentials(restore_transport):
    audible_client.configure_transport("http://opsuser:s3cr3t-token@libex-seeder-vpn:8888")
    summary = audible_client.transport_summary()

    assert summary.host == "libex-seeder-vpn"
    text = repr(summary) + str(summary)
    assert "opsuser" not in text
    assert "s3cr3t-token" not in text
    assert "http://" not in text


# ============================================================
# RECONFIGURING -- REBUILDS THE CLIENT, CLOSES THE OLD ONE
# ============================================================

@pytest.mark.asyncio
async def test_reconfiguring_rebuilds_the_client_and_closes_the_old_one(restore_transport):
    audible_client.configure_transport("http://libex-one-vpn:8888")
    audible_client._audible_client = None
    audible_client._audible_client_loop = None
    audible_client._audible_client_generation = None
    first = audible_client._get_audible_client()

    created_tasks = []
    real_create_task = asyncio.create_task

    def _tracking_create_task(coro, **kwargs):
        task = real_create_task(coro, **kwargs)
        created_tasks.append(task)
        return task

    with patch("libex_core.audible.client.asyncio.create_task", side_effect=_tracking_create_task):
        audible_client.configure_transport("http://libex-two-vpn:8888")
        second = audible_client._get_audible_client()

    assert second is not first
    # _close_stale_client is fired via asyncio.create_task, not awaited
    # inline -- wait for it explicitly rather than guessing at a sleep(0).
    await asyncio.gather(*created_tasks)
    assert first.is_closed
    await second.aclose()


# ============================================================
# audible_get -- FETCHES THE CLIENT PER ATTEMPT, NOT ONCE BEFORE THE LOOP
# ============================================================

@pytest.mark.asyncio
async def test_audible_get_does_not_reuse_a_client_closed_by_a_mid_retry_reconfigure(
    restore_transport,
):
    """audible_get's own docstring is explicit about why the client is
    fetched inside the retry loop rather than once before it: a reconfigure
    landing between two attempts of the same call bumps the transport
    generation, and if that bump has by then caused the old client to be
    closed, calling .get() on it raises a plain RuntimeError -- not one of
    the exceptions this function retries -- so it would escape uncaught
    rather than being retried.

    Proved with a real httpx.AsyncClient over an in-process MockTransport,
    not a mocked .get(): the closed-client check this is protecting against
    lives inside AsyncClient.send() itself, so a mocked .get() that replaces
    the whole method would never exercise it and this would pass whether or
    not the fetch-per-attempt behavior was actually there."""
    audible_client.configure_transport("http://libex-one-vpn:8888")
    audible_client._audible_client = None
    audible_client._audible_client_loop = None
    audible_client._audible_client_generation = None

    call_count = {"n": 0}

    def _handler(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    real_async_client_cls = httpx.AsyncClient

    def _fake_async_client(*args, **kwargs):
        # proxy= would otherwise mount a REAL AsyncHTTPTransport ahead of the
        # MockTransport for every URL (httpx checks _mounts before the
        # client's own base _transport), which would try a genuine
        # connection to the fake proxy host and fail on DNS resolution
        # rather than exercising the fake handler below.
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async_client_cls(*args, **kwargs)

    async def _reconfigure_and_close_the_stale_client(_seconds):
        # Simulates the real background close (_close_stale_client, fired
        # via asyncio.create_task from _get_audible_client) having already
        # completed by the time the next attempt runs, rather than racing
        # this test's own event loop for it.
        stale = audible_client._audible_client
        audible_client.configure_transport("http://libex-two-vpn:8888")
        await stale.aclose()

    with patch("libex_core.audible.client.httpx.AsyncClient", side_effect=_fake_async_client), \
         patch(
             "libex_core.audible.client.asyncio.sleep",
             new=AsyncMock(side_effect=_reconfigure_and_close_the_stale_client),
         ):
        result = await audible_client.audible_get("us", "/1.0/catalog/products", {"page": 0})

    assert result == {"ok": True}
    assert call_count["n"] == 2


# ============================================================
# UNCONFIGURED -- REFUSED, NOT DEFAULTED TO DIRECT EGRESS
#
# _transport starts life at mode="unconfigured", and building a client from
# that state produces proxy=None -- at the wire, indistinguishable from a
# deliberate configure_transport(None) direct-egress choice. The guard in
# _get_audible_client exists so an embedder that never calls
# configure_transport() at all gets a loud refusal instead of silent direct
# egress over whatever IP the host happens to have. mode="direct" (from an
# explicit None or "") and mode="proxy" are both deliberately untouched by
# this guard -- see the two sections below for each.
# ============================================================

def _force_unconfigured():
    """Overwrites the module's transport with a fresh 'unconfigured'
    snapshot, the same shape _transport starts at before any
    configure_transport() call in this process. configure_transport() itself
    has no way to produce this state again once past it -- None/"" land on
    "direct", not "unconfigured" -- so the snapshot is built directly, the
    same pattern tests/scripts/conftest.py's restore_audible_transport
    callers already use for this exact state."""
    audible_client._transport = audible_client._TransportSnapshot(
        mode="unconfigured", proxy=None, host=None
    )
    audible_client._audible_client = None
    audible_client._audible_client_loop = None
    audible_client._audible_client_generation = None


def test_get_audible_client_raises_runtime_error_when_unconfigured(restore_transport):
    _force_unconfigured()

    with pytest.raises(RuntimeError, match="Audible transport has not been configured"):
        audible_client._get_audible_client()


@pytest.mark.asyncio
async def test_audible_get_raises_runtime_error_before_any_network_call_when_unconfigured(
    restore_transport,
):
    """Pinned to RuntimeError specifically, not AudibleAPIException or
    NotFoundException -- neither of those is raised here, and both would
    misrepresent this as an answer from Audible rather than a refusal to
    ever ask. httpx.AsyncClient itself is patched to blow up if constructed
    at all, which is what proves the refusal happens before any client --
    and so before any socket -- exists, rather than merely before a request
    the client would have sent."""
    _force_unconfigured()

    with patch(
        "libex_core.audible.client.httpx.AsyncClient",
        side_effect=AssertionError(
            "no httpx.AsyncClient should be built while the transport is unconfigured"
        ),
    ) as mock_client_cls:
        with pytest.raises(RuntimeError, match="Audible transport has not been configured"):
            await audible_client.audible_get("us", "/1.0/catalog/products")

    mock_client_cls.assert_not_called()
    # The refusal doesn't consume or alter the state it found -- a caller
    # that catches this and calls transport_summary() to report it (as a
    # startup healthcheck might) sees the same "unconfigured" it just failed
    # against, not something the failure itself mutated.
    assert audible_client.transport_summary().mode == "unconfigured"


@pytest.mark.asyncio
async def test_get_audible_client_unaffected_by_direct_egress_from_none(restore_transport):
    audible_client.configure_transport(None, allow_direct_egress=True)
    audible_client._audible_client = None
    audible_client._audible_client_loop = None
    audible_client._audible_client_generation = None

    client = audible_client._get_audible_client()
    try:
        assert client._mounts == {}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_audible_client_unaffected_by_direct_egress_from_empty_string(restore_transport):
    audible_client.configure_transport("", allow_direct_egress=True)
    audible_client._audible_client = None
    audible_client._audible_client_loop = None
    audible_client._audible_client_generation = None

    client = audible_client._get_audible_client()
    try:
        assert client._mounts == {}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_audible_client_unaffected_by_proxy_mode(restore_transport):
    audible_client.configure_transport("http://libex-one-vpn:8888")
    audible_client._audible_client = None
    audible_client._audible_client_loop = None
    audible_client._audible_client_generation = None

    client = audible_client._get_audible_client()
    try:
        assert client._mounts != {}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_audible_get_refuses_on_the_next_attempt_after_a_mid_retry_reconfigure_to_unconfigured(
    restore_transport,
):
    """The guard is read inside the retry loop, once per attempt (see
    _get_audible_client's own docstring on why it's fetched there rather
    than once before it) -- so a transport that goes from configured to
    unconfigured between two attempts of the same call must refuse on the
    very next attempt rather than carrying on with whatever client the first
    attempt already built. Modeled on
    test_audible_get_does_not_reuse_a_client_closed_by_a_mid_retry_reconfigure
    above: the first attempt gets a real 429 over a MockTransport, and the
    fake asyncio.sleep between attempts is where the transport is knocked
    back to unconfigured, simulating a reconfigure racing a pending retry."""
    audible_client.configure_transport("http://libex-one-vpn:8888")
    audible_client._audible_client = None
    audible_client._audible_client_loop = None
    audible_client._audible_client_generation = None

    call_count = {"n": 0}

    def _handler(request):
        call_count["n"] += 1
        return httpx.Response(429)

    real_async_client_cls = httpx.AsyncClient

    def _fake_async_client(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async_client_cls(*args, **kwargs)

    async def _force_unconfigured_between_attempts(_seconds):
        _force_unconfigured()

    with patch("libex_core.audible.client.httpx.AsyncClient", side_effect=_fake_async_client), \
         patch(
             "libex_core.audible.client.asyncio.sleep",
             new=AsyncMock(side_effect=_force_unconfigured_between_attempts),
         ):
        with pytest.raises(RuntimeError, match="Audible transport has not been configured"):
            await audible_client.audible_get("us", "/1.0/catalog/products")

    # Exactly one real request went out -- the first attempt, before the
    # transport was knocked back. The second attempt refused before ever
    # reaching client.get(), so the retryable 429 never got a chance to be
    # retried into a third.
    assert call_count["n"] == 1
    assert audible_client.transport_summary().mode == "unconfigured"


# ============================================================
# IMPORT-TIME ISOLATION -- a .env in the importing directory reads nothing
# ============================================================

_ENV_DIR_CHILD_SCRIPT = """
import libex_core.audible.client as client

summary = client.transport_summary()
print("MODE:" + summary.mode)
print("HOST:" + (summary.host or ""))
"""


def test_importing_the_client_from_a_dotenv_holding_directory_reads_nothing(tmp_path):
    """libex_core reads no environment and no settings of its own (see the
    module's own docstring) -- merely importing it from a working directory
    that happens to hold a .env naming AUDIBLE_PROXY_URL must not configure
    anything from it. Run in a subprocess from that directory, the same
    isolation technique test_isolation.py uses, since only a genuinely fresh
    interpreter proves this rather than one that already has libex_core (and
    its module-level transport singleton) loaded from an earlier test."""
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
    assert "MODE:unconfigured" in result.stdout
    assert "should-never-be-read" not in result.stdout
