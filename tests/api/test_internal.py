"""
Internal seed endpoint tests.
Tests auth, validation, and narrator matching.
"""

# Standard library
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
from app.api.routes.internal.router import hash_secret


SEED_TOKEN = "test-secret-token-12345"
SEED_HASH = hash_secret(SEED_TOKEN)


# ============================================================
# AUTH TESTS
# ============================================================


@pytest.mark.asyncio
async def test_seed_narrators_returns_401_without_token(async_client):
    """Returns 401 when no authorization header is provided."""
    with patch("app.api.routes.internal.router.settings") as mock_settings:
        mock_settings.seed_secret = SEED_HASH
        response = await async_client.post("/internal/seed/narrators", json=[])
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_seed_narrators_returns_401_with_wrong_token(async_client):
    """Returns 401 when token doesn't match."""
    with patch("app.api.routes.internal.router.settings") as mock_settings:
        mock_settings.seed_secret = SEED_HASH
        response = await async_client.post(
            "/internal/seed/narrators",
            json=[],
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_seed_narrators_returns_401_when_secret_not_set(async_client):
    """Returns 401 when SEED_SECRET env var is empty."""
    with patch("app.api.routes.internal.router.settings") as mock_settings:
        mock_settings.seed_secret = ""
        response = await async_client.post(
            "/internal/seed/narrators",
            json=[],
            headers={"Authorization": "Bearer some-token"},
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_seed_narrators_returns_401_with_malformed_header(async_client):
    """Returns 401 when Authorization header is malformed."""
    with patch("app.api.routes.internal.router.settings") as mock_settings:
        mock_settings.seed_secret = SEED_HASH
        response = await async_client.post(
            "/internal/seed/narrators",
            json=[],
            headers={"Authorization": SEED_TOKEN},
        )
        assert response.status_code == 401


# ============================================================
# VALIDATION TESTS
# ============================================================


@pytest.mark.asyncio
async def test_seed_narrators_returns_400_for_non_array(async_client):
    """Returns 400 when body is not a JSON array."""
    with patch("app.api.routes.internal.router.settings") as mock_settings:
        mock_settings.seed_secret = SEED_HASH
        response = await async_client.post(
            "/internal/seed/narrators",
            json={"name": "Jim Dale"},
            headers={"Authorization": f"Bearer {SEED_TOKEN}"},
        )
        assert response.status_code == 400
        assert "Expected JSON array" in response.json()["error"]


@pytest.mark.asyncio
async def test_seed_narrators_returns_200_with_empty_array(async_client):
    """Returns 200 with zero counts for empty input."""
    mock_session = AsyncMock()
    with patch("app.api.routes.internal.router.settings") as mock_settings, \
         patch("app.api.routes.internal.router.get_session", return_value=mock_session):
        mock_settings.seed_secret = SEED_HASH
        response = await async_client.post(
            "/internal/seed/narrators",
            json=[],
            headers={"Authorization": f"Bearer {SEED_TOKEN}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["matched"] == 0
        assert data["skipped"] == 0


@pytest.mark.asyncio
async def test_seed_narrators_skips_entries_without_name(async_client):
    """Entries without a name field are skipped."""
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    with patch("app.api.routes.internal.router.settings") as mock_settings, \
         patch("app.api.routes.internal.router.get_session", return_value=mock_session):
        mock_settings.seed_secret = SEED_HASH
        response = await async_client.post(
            "/internal/seed/narrators",
            json=[{"description": "No name here"}],
            headers={"Authorization": f"Bearer {SEED_TOKEN}"},
        )
        assert response.status_code == 200
        assert response.json()["skipped"] == 1


# ============================================================
# CRYPTO TESTS
# ============================================================


def test_hash_secret_produces_different_hashes():
    """Same plaintext produces different hashes (random salt)."""
    h1 = hash_secret("test")
    h2 = hash_secret("test")
    assert h1 != h2


def test_verify_secret_validates_correct_token():
    """Correct plaintext verifies against its hash."""
    from app.api.routes.internal.router import verify_secret
    hashed = hash_secret("my-secret")
    assert verify_secret("my-secret", hashed) is True


def test_verify_secret_rejects_wrong_token():
    """Wrong plaintext fails verification."""
    from app.api.routes.internal.router import verify_secret
    hashed = hash_secret("my-secret")
    assert verify_secret("wrong-secret", hashed) is False


def test_verify_secret_rejects_garbage_hash():
    """Garbage hash string fails gracefully."""
    from app.api.routes.internal.router import verify_secret
    assert verify_secret("anything", "not-a-valid-hash") is False


def test_verify_secret_rejects_empty_hash():
    """Empty hash fails gracefully."""
    from app.api.routes.internal.router import verify_secret
    assert verify_secret("anything", "") is False


# ============================================================
# ENDPOINT VISIBILITY
# ============================================================


def test_seed_endpoint_not_in_openapi(async_client):
    """Seed endpoint is not visible in the OpenAPI schema."""
    from app.main import app
    schema = app.openapi()
    paths = schema.get("paths", {})
    assert "/internal/seed/narrators" not in paths