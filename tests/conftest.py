"""
Test configuration and fixtures.
"""

# Standard library
from unittest.mock import AsyncMock

# Third party
import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport

# Local
from app.main import app


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