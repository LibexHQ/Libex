"""
What `LocalStore` hands asyncpg for a Postgres URL: every connection
parameter, so the driver reads nothing from `PG*` variables, `~/.pgpass` or
`~/.postgresql`; and which failures let it try the other transport. No
database needed; the real-server proof is in test_store_postgres_environment.
"""

# Standard library
import asyncio
import ssl
import sys

# Third party
import asyncpg
import pytest

# Local
from libex_core.storage import store as store_module
from libex_core.storage.store import (
    LocalStore,
    StoreConfigError,
    StoreConnectionError,
    _postgres_creator,
    _validate,
)

BASE = "postgresql+asyncpg://u:pw@db.example:6543/d"

# What asyncpg raises when the server answers the SSLRequest with a refusal.
TLS_REFUSED = ConnectionError("rejected SSL upgrade")
# What a server that insists on encryption answers a plain attempt with.
NOT_ENCRYPTED = asyncpg.InvalidAuthorizationSpecificationError("no encryption")


class _Fake:
    def __init__(self, refuse=()):
        self.calls = []
        self.refuse = list(refuse)

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.refuse:
            raise self.refuse.pop(0)
        return "connection"


async def _attempt(monkeypatch, url, refuse=()):
    """Runs the creator; returns the calls made and what it raised, if anything."""
    fake = _Fake(refuse)
    monkeypatch.setattr(asyncpg, "connect", fake)
    try:
        await _postgres_creator(_validate(url))()
    except Exception as exc:
        return fake.calls, exc
    return fake.calls, None


async def _connect(monkeypatch, url, refuse=()):
    """The calls made. An error the creator raises fails the test, so a
    helper never hides one."""
    calls, exc = await _attempt(monkeypatch, url, refuse)
    if exc is not None:
        raise exc
    return calls


def _transports(calls):
    """True for each encrypted attempt, False for each plain one."""
    return [call["ssl"] is not False for call in calls]


async def test_every_parameter_is_given_so_the_driver_has_nothing_to_look_up(monkeypatch):
    (call,) = await _connect(monkeypatch, BASE + "?ssl=require")
    assert call["host"] == "db.example" and call["port"] == 6543
    assert (call["user"], call["password"], call["database"]) == ("u", "pw", "d")
    assert isinstance(call["ssl"], ssl.SSLContext)
    # Each of these is read from the environment by asyncpg when left as None.
    assert call["direct_tls"] is False
    assert call["target_session_attrs"] == "any"
    assert call["krbsrvname"] == "postgres"
    assert call["gsslib"] == ("sspi" if sys.platform == "win32" else "gssapi")
    assert call["server_settings"] is None
    assert "dsn" not in call and "passfile" not in call and "servicefile" not in call


async def test_a_url_without_a_password_connects_with_an_empty_one_not_none(monkeypatch):
    (call,) = await _connect(monkeypatch, "postgresql+asyncpg://u@db.example/d?ssl=disable")
    assert call["password"] == "" and call["port"] == 5432 and call["ssl"] is False


async def test_the_default_is_encrypt_if_offered_then_plain(monkeypatch):
    first, second = await _connect(monkeypatch, BASE, refuse=[TLS_REFUSED])
    assert isinstance(first["ssl"], ssl.SSLContext) and second["ssl"] is False
    assert first["ssl"].verify_mode == ssl.CERT_NONE


async def test_require_and_verify_never_fall_back_to_plain(monkeypatch):
    for mode in ("require", "verify-ca", "verify-full"):
        for refusal in (TLS_REFUSED, NOT_ENCRYPTED, ConnectionResetError("x")):
            calls, exc = await _attempt(monkeypatch, f"{BASE}?ssl={mode}", refuse=[refusal])
            assert _transports(calls) == [True], mode
            assert exc is refusal, mode


async def test_disable_makes_one_plain_attempt(monkeypatch):
    calls, exc = await _attempt(monkeypatch, BASE + "?ssl=disable", refuse=[NOT_ENCRYPTED])
    assert _transports(calls) == [False] and exc is NOT_ENCRYPTED


async def test_verify_modes_verify_and_the_others_do_not(monkeypatch):
    (ca,) = await _connect(monkeypatch, BASE + "?ssl=verify-ca")
    (full,) = await _connect(monkeypatch, BASE + "?ssl=verify-full")
    (req,) = await _connect(monkeypatch, BASE + "?ssl=require")
    (pref,) = await _connect(monkeypatch, BASE)
    assert (ca["ssl"].verify_mode, ca["ssl"].check_hostname) == (ssl.CERT_REQUIRED, False)
    assert (full["ssl"].verify_mode, full["ssl"].check_hostname) == (ssl.CERT_REQUIRED, True)
    assert (req["ssl"].verify_mode, req["ssl"].check_hostname) == (ssl.CERT_NONE, False)
    assert (pref["ssl"].verify_mode, pref["ssl"].check_hostname) == (ssl.CERT_NONE, False)
    assert full["ssl"].minimum_version >= ssl.TLSVersion.TLSv1_2


async def test_only_the_verifying_modes_load_the_system_trust_store(monkeypatch):
    loaded = []
    monkeypatch.setattr(
        ssl.SSLContext, "load_default_certs", lambda self, *a, **k: loaded.append(self)
    )
    for mode, expected in (
        ("verify-ca", 1), ("verify-full", 1), ("require", 0), ("allow", 0), ("disable", 0),
    ):
        loaded.clear()
        await _connect(monkeypatch, f"{BASE}?ssl={mode}")
        assert len(loaded) == expected, mode
    loaded.clear()
    await _connect(monkeypatch, BASE)
    assert loaded == []


async def test_allow_tries_plain_first(monkeypatch):
    first, second = await _connect(monkeypatch, BASE + "?ssl=allow", refuse=[NOT_ENCRYPTED])
    assert first["ssl"] is False and isinstance(second["ssl"], ssl.SSLContext)


@pytest.mark.parametrize("refusal", [
    ConnectionRefusedError("refused"),
    ConnectionResetError("reset"),
    ConnectionError("unexpected connection_lost() call"),
    ssl.SSLError("handshake"),
    asyncpg.InvalidPasswordError("bad"),
    asyncpg.InvalidAuthorizationSpecificationError("no encryption"),
    ValueError("boom"),
])
async def test_prefer_retries_plain_only_when_the_server_refused_tls(monkeypatch, refusal):
    calls, exc = await _attempt(monkeypatch, BASE, refuse=[refusal])
    assert _transports(calls) == [True]
    assert exc is refusal


async def test_prefer_retries_plain_on_the_refusal_signal_alone(monkeypatch):
    calls, exc = await _attempt(monkeypatch, BASE, refuse=[TLS_REFUSED])
    assert _transports(calls) == [True, False] and exc is None


async def test_a_wrong_password_is_never_sent_again_over_plain_on_prefer(monkeypatch):
    bad = asyncpg.InvalidPasswordError("password authentication failed")
    calls, exc = await _attempt(monkeypatch, BASE, refuse=[bad])
    assert _transports(calls) == [True]
    assert exc is bad


async def test_a_wrong_password_ends_allow_after_the_plain_attempt(monkeypatch):
    bad = asyncpg.InvalidPasswordError("password authentication failed")
    calls, exc = await _attempt(monkeypatch, BASE + "?ssl=allow", refuse=[bad])
    assert _transports(calls) == [False]
    assert exc is bad


@pytest.mark.parametrize("refusal", [
    ConnectionRefusedError("refused"), ConnectionResetError("reset"), TLS_REFUSED,
])
async def test_allow_retries_encrypted_only_when_the_server_wants_encryption(monkeypatch, refusal):
    calls, exc = await _attempt(monkeypatch, BASE + "?ssl=allow", refuse=[refusal])
    assert _transports(calls) == [False] and exc is refusal


async def test_a_wrong_password_reaches_the_caller_as_a_connection_error_with_one_attempt(monkeypatch):
    fake = _Fake([asyncpg.InvalidPasswordError("password authentication failed for hunter2")])
    monkeypatch.setattr(asyncpg, "connect", fake)
    store = LocalStore(BASE)
    try:
        with pytest.raises(StoreConnectionError) as caught:
            await store.status()
    finally:
        await store.close()
    assert _transports(fake.calls) == [True]
    assert "InvalidPasswordError" in str(caught.value) and "hunter2" not in str(caught.value)


async def test_an_unrelated_error_is_not_retried(monkeypatch):
    calls, exc = await _attempt(monkeypatch, BASE, refuse=[ValueError("boom")])
    assert len(calls) == 1 and isinstance(exc, ValueError)


async def test_application_name_goes_in_as_a_server_setting(monkeypatch):
    (call,) = await _connect(monkeypatch, BASE + "?ssl=require&application_name=mine")
    assert call["server_settings"] == {"application_name": "mine"}


def test_an_unknown_ssl_mode_is_refused_without_quoting_it():
    with pytest.raises(StoreConfigError) as caught:
        LocalStore(BASE + "?ssl=hunter2")
    assert "hunter2" not in str(caught.value)


def test_the_attempt_table_matches_the_documented_modes():
    assert store_module._TLS_ATTEMPTS == {
        "disable": (False,),
        "allow": (False, True),
        "prefer": (True, False),
        "require": (True,),
        "verify-ca": (True,),
        "verify-full": (True,),
    }


@pytest.mark.integration
async def test_asyncpg_still_words_a_refused_sslrequest_the_way_prefer_reads_it():
    """`_may_retry` recognises a refused SSLRequest by the text asyncpg gives
    its `ConnectionError`. A real server answering 'N' pins that text, so an
    asyncpg release that rewords it fails here instead of silently turning off
    the fallback that `prefer` relies on. Marked integration only to open the
    local socket the unit-test network guard otherwise blocks; it needs no
    database."""
    seen = []

    async def refuse(reader, writer):
        seen.append(await reader.readexactly(8))
        writer.write(b"N")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(refuse, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(Exception) as caught:
            await asyncpg.connect(
                host="127.0.0.1",
                port=port,
                user="u",
                password="pw",
                database="d",
                ssl=ssl.create_default_context(),
                timeout=5,
            )
    finally:
        server.close()
        await server.wait_closed()

    assert seen, "the server was never sent an SSLRequest"
    assert type(caught.value) is ConnectionError
    assert "rejected SSL upgrade" in str(caught.value)
    assert store_module._may_retry(True, caught.value) is True
