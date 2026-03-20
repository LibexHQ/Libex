"""
Health endpoint tests.
"""

# Third party
import pytest
from fastapi.testclient import TestClient

# Local
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_health_returns_ok(client):
    """Health endpoint returns 200 with correct structure."""
    response = client.get("/health")
    assert response.status_code == 200


def test_health_returns_version(client):
    """Health endpoint returns app version."""
    response = client.get("/health")
    data = response.json()
    assert "version" in data
    assert "status" in data


def test_health_status_is_ok(client):
    """Health endpoint status field is ok."""
    response = client.get("/health")
    data = response.json()
    assert data["status"] == "ok"


def test_health_version_format(client):
    """Health endpoint version follows semver format."""
    response = client.get("/health")
    data = response.json()
    parts = data["version"].split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)