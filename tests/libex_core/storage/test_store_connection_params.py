"""
What `LocalStore` hands asyncpg for a Postgres URL: every connection
parameter, so the driver reads nothing from `PG*` variables, `~/.pgpass` or
`~/.postgresql`. No database needed; the real-server proof is in
test_store_postgres_environment.
"""

# Standard library
import ssl

# Third party
import asyncpg
import pytest

# Local
from libex_core.storage.store import LocalStore, StoreConfigError, _postgres_creator, _validate

BASE = "postgresql+asyncpg://u:pw@db.example:6543/d"


class _Fake:
    def __init__(self, refuse=()):
        self.calls = []
        self.refuse = list(refuse)

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.refuse:
            raise self.refuse.pop(0)
        return "connection"


async def _connect(monkeypatch, url, refuse=()):
    fake = _Fake(refuse)
    monkeypatch.setattr(asyncpg, "connect", fake)
    try:
        await _postgres_creator(_validate(url))()
    finally:
        return fake.calls


async def test_every_parameter_is_given_so_the_driver_has_nothing_to_look_up(monkeypatch):
    (call,) = await _connect(monkeypatch, BASE + "?ssl=require")
    assert call["host"] == "db.example" and call["port"] == 6543
    assert (call["user"], call["password"], call["database"]) == ("u", "pw", "d")
    assert isinstance(call["ssl"], ssl.SSLContext)
    # Each of these is read from the environment by asyncpg when left as None.
    for name in ("direct_tls", "target_session_attrs", "krbsrvname", "gsslib"):
        assert call[name] is not None, name
    assert "dsn" not in call and "passfile" not in call and "servicefile" not in call


async def test_a_url_without_a_password_connects_with_an_empty_one_not_none(monkeypatch):
    (call,) = await _connect(monkeypatch, "postgresql+asyncpg://u@db.example/d?ssl=disable")
    assert call["password"] == "" and call["port"] == 5432 and call["ssl"] is False


async def test_the_default_is_encrypt_if_offered_then_plain(monkeypatch):
    first, second = await _connect(monkeypatch, BASE, refuse=[ConnectionError("no tls")])
    assert isinstance(first["ssl"], ssl.SSLContext) and second["ssl"] is False
    assert first["ssl"].verify_mode == ssl.CERT_NONE


async def test_require_and_verify_never_fall_back_to_plain(monkeypatch):
    for mode in ("require", "verify-ca", "verify-full"):
        calls = await _connect(monkeypatch, f"{BASE}?ssl={mode}", refuse=[ConnectionError("no tls")])
        assert len(calls) == 1, mode


async def test_verify_modes_verify_and_the_others_do_not(monkeypatch):
    (ca,) = await _connect(monkeypatch, BASE + "?ssl=verify-ca")
    (full,) = await _connect(monkeypatch, BASE + "?ssl=verify-full")
    (req,) = await _connect(monkeypatch, BASE + "?ssl=require")
    assert (ca["ssl"].verify_mode, ca["ssl"].check_hostname) == (ssl.CERT_REQUIRED, False)
    assert (full["ssl"].verify_mode, full["ssl"].check_hostname) == (ssl.CERT_REQUIRED, True)
    assert (req["ssl"].verify_mode, req["ssl"].check_hostname) == (ssl.CERT_NONE, False)
    assert full["ssl"].minimum_version >= ssl.TLSVersion.TLSv1_2


async def test_allow_tries_plain_first(monkeypatch):
    first, second = await _connect(monkeypatch, BASE + "?ssl=allow", refuse=[ConnectionError("x")])
    assert first["ssl"] is False and isinstance(second["ssl"], ssl.SSLContext)


async def test_an_unrelated_error_is_not_retried(monkeypatch):
    calls = await _connect(monkeypatch, BASE, refuse=[ValueError("boom")])
    assert len(calls) == 1


async def test_application_name_goes_in_as_a_server_setting(monkeypatch):
    (call,) = await _connect(monkeypatch, BASE + "?ssl=require&application_name=mine")
    assert call["server_settings"] == {"application_name": "mine"}


def test_an_unknown_ssl_mode_is_refused_without_quoting_it():
    with pytest.raises(StoreConfigError) as caught:
        LocalStore(BASE + "?ssl=hunter2")
    assert "hunter2" not in str(caught.value)
