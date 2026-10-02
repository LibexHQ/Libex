"""
The proof that a Postgres `LocalStore` takes its settings from the URL alone.

Every `PG*` variable is set to a value that would break or redirect a
connection, a `~/.pgpass` is planted, and the store still connects; and in the
other direction a URL with no password does not pick up one offered through
`PGPASSWORD` or `~/.pgpass`. Each case has a control that shows plain asyncpg
does read the environment, so a pass cannot be an environment that was never
consulted. Needs Docker, through the container fixture in conftest.
"""

# Standard library
import os

# Third party
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Local
from libex_core.storage.store import LocalStore, StoreConnectionError

pytestmark = pytest.mark.integration

# Every variable libpq documents that asyncpg reads, plus the TLS keylog one.
HOSTILE = {
    "PGHOST": "203.0.113.1",
    "PGPORT": "1",
    "PGUSER": "nobody",
    "PGDATABASE": "nonexistent",
    "PGPASSWORD": "wrong-password",
    "PGSSLMODE": "verify-full",
    "PGSSLROOTCERT": "/nonexistent/root.crt",
    "PGSSLCERT": "/nonexistent/client.crt",
    "PGSSLKEY": "/nonexistent/client.key",
    "PGSSLCRL": "/nonexistent/root.crl",
    "PGSSLNEGOTIATION": "direct",
    "PGSSLMINPROTOCOLVERSION": "TLSv1.3",
    "PGSSLMAXPROTOCOLVERSION": "TLSv1.0",
    "PGTARGETSESSIONATTRS": "read-only",
    "PGSERVICE": "nowhere",
    "PGSERVICEFILE": "/nonexistent/service.conf",
    "PGPASSFILE": "/nonexistent/pgpass",
    "PGKRBSRVNAME": "nobody",
    "PGGSSLIB": "bogus",
    "SSLKEYLOGFILE": "/nonexistent/keys.log",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _hostile(monkeypatch):
    for name, value in HOSTILE.items():
        monkeypatch.setenv(name, value)


async def _store_can_query(url) -> bool:
    store = LocalStore(url)
    try:
        async with store._engine.connect() as connection:
            return (await connection.execute(text("SELECT 1"))).scalar() == 1
    finally:
        await store.close()


async def _plain_can_query(url) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text("SELECT 1"))).scalar() == 1
    finally:
        await engine.dispose()


async def test_every_pg_variable_set_to_garbage_changes_nothing(postgres_url, home, monkeypatch):
    # Control: the same variables do break plain asyncpg on this very URL.
    with monkeypatch.context() as hostile:
        _hostile(hostile)
        with pytest.raises(Exception):
            await _plain_can_query(postgres_url)
        assert await _store_can_query(postgres_url) is True


async def test_a_url_without_a_password_does_not_pick_one_up_from_pgpassword(postgres_url, home, monkeypatch):
    parsed = make_url(postgres_url)
    bare = parsed._replace(password=None).render_as_string(hide_password=False)
    monkeypatch.setenv("PGPASSWORD", parsed.password)
    # Control: asyncpg takes the password from the variable and gets in.
    assert await _plain_can_query(bare) is True
    store = LocalStore(bare)
    try:
        with pytest.raises(StoreConnectionError):
            await store.status()
    finally:
        await store.close()


async def test_a_url_without_a_password_does_not_read_pgpass(postgres_url, home, monkeypatch):
    parsed = make_url(postgres_url)
    bare = parsed._replace(password=None).render_as_string(hide_password=False)
    pgpass = home / ".pgpass"
    pgpass.write_text(f"*:*:*:{parsed.username}:{parsed.password}\n")
    os.chmod(pgpass, 0o600)
    assert await _plain_can_query(bare) is True
    store = LocalStore(bare)
    try:
        with pytest.raises(StoreConnectionError):
            await store.status()
    finally:
        await store.close()


async def test_the_password_in_the_url_wins_over_a_wrong_pgpassword_and_pgpass(postgres_url, home, monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "wrong-password")
    (home / ".pgpass").write_text("*:*:*:*:also-wrong\n")
    os.chmod(home / ".pgpass", 0o600)
    assert await _store_can_query(postgres_url) is True


async def test_pgsslmode_does_not_change_how_the_store_negotiates(postgres_url, home, monkeypatch):
    # The container offers TLS or not; either way the URL default (prefer)
    # connects, and a stricter variable must not stop it.
    monkeypatch.setenv("PGSSLMODE", "verify-full")
    assert await _store_can_query(postgres_url) is True
