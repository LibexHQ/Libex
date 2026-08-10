"""
Test configuration and fixtures.
"""

# Standard library
import socket
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport

# Local
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def mock_alembic_upgrade():
    """Skip real migrations during tests. Lifespan startup runs alembic upgrade
    against the configured DATABASE_URL; CI has no Postgres, and unit tests
    shouldn't depend on one. Patching at the import location in app.main
    keeps lifespan itself exercised."""
    with patch("app.main.command.upgrade"):
        yield


@pytest.fixture(autouse=True)
def block_network_sockets(request):
    """
    Fail loudly on any real outbound socket connection made by a unit test.

    A test that forgets to patch an outbound call (audible_get, a raw
    httpx.AsyncClient, etc.) should error immediately naming the attempted
    host, not silently reach the real network. httpx's ASGITransport (the
    `client`/`async_client` fixtures) never opens a socket — it calls the
    ASGI app in-process — so this does not touch them.

    Blocking is done at the socket layer (socket.socket.connect and
    socket.create_connection), not by patching httpx, because patching httpx
    only catches call sites that go through httpx; a stray requests/asyncpg/
    raw-socket call would sail through a patch scoped to httpx.

    Tests marked `integration` are exempted: they legitimately open real
    sockets (a testcontainers Postgres instance on localhost). That is the
    sanctioned, greppable opt-out (`@pytest.mark.integration`) — nothing else
    should need one.
    """
    if request.node.get_closest_marker("integration"):
        yield
        return

    fixture_name = "block_network_sockets (tests/conftest.py)"

    def _refuse(host, port):
        raise RuntimeError(
            f"Real network connection attempted to {host}:{port} during "
            f"test '{request.node.nodeid}'. Blocked by the {fixture_name} "
            "autouse fixture. Mock the outbound call at its import location "
            "(e.g. patch `audible_get` where the caller imports it) instead "
            "of letting it reach the network. If this test genuinely needs "
            "a real socket, mark it `@pytest.mark.integration`."
        )

    def guard_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        port = address[1] if isinstance(address, tuple) and len(address) > 1 else "?"
        _refuse(host, port)

    def guard_create_connection(address, *args, **kwargs):
        host, port = address[0], address[1]
        _refuse(host, port)

    with (
        patch.object(socket.socket, "connect", guard_connect),
        patch.object(socket, "create_connection", guard_create_connection),
    ):
        yield


@pytest.fixture
def client():
    """Synchronous test client for simple endpoint tests."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
async def async_client():
    """Async test client for async endpoint tests."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
def mock_db_session():
    """Mock database session that does nothing."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=AsyncMock(scalar_one_or_none=AsyncMock(return_value=None)))
    session.commit = AsyncMock()
    return session